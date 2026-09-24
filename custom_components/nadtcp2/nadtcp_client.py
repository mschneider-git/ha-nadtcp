"""Vendored, Python 3.10+ compatible fork of the 'nadtcp' PyPI package (0.2.0.dev2).

The original package calls asyncio.wait_for()/asyncio.sleep() with a `loop=`
keyword argument, which was removed in Python 3.10. That made connect() raise
a TypeError immediately, so the entity never received any state and stayed
'unavailable'. This vendored copy drops the removed keyword arguments and
fixes an `is`-vs-`==` string comparison.

It also protects the amplifier's connection slots: the C338 firmware does not
free the slots of closed connections, so a burst of reconnects locks it up
until it is power-cycled. Unparseable lines are skipped instead of dropping
the connection, and every reconnect waits, backing off when connections keep
dying quickly.
"""
import asyncio
import socket
import logging
import time

_LOGGER = logging.getLogger(__name__)

CMD_MAIN = "Main"
CMD_BRIGHTNESS = "Main.Brightness"
CMD_BASS_EQ = "Main.Bass"
CMD_CONTROL_STANDBY = "Main.ControlStandby"
CMD_AUTO_STANDBY = "Main.AutoStandby"
CMD_VERSION = "Main.Version"
CMD_MUTE = "Main.Mute"
CMD_POWER = "Main.Power"
CMD_AUTO_SENSE = "Main.AutoSense"
CMD_SOURCE = "Main.Source"
CMD_VOLUME = "Main.Volume"

MSG_ON = 'On'
MSG_OFF = 'Off'

C338_CMDS = {
    'Main':
        {'supported_operators': ['?']
         },
    'Main.AnalogGain':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': range(0, 0),
         'type': int
         },
    'Main.Brightness':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': range(0, 4),
         'type': int
         },
    'Main.Mute':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': [MSG_OFF, MSG_ON],
         'type': bool
         },
    'Main.Power':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': [MSG_OFF, MSG_ON],
         'type': bool
         },
    'Main.Volume':
        {'supported_operators': ['+', '-', '=', '?'],
         'min': -80,
         'max': 0,
         'type': float
         },
    'Main.Bass':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': [MSG_OFF, MSG_ON],
         'type': bool
         },
    'Main.ControlStandby':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': [MSG_OFF, MSG_ON],
         'type': bool
         },
    'Main.AutoStandby':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': [MSG_OFF, MSG_ON],
         'type': bool
         },
    'Main.AutoSense':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': [MSG_OFF, MSG_ON],
         'type': bool
         },
    'Main.Source':
        {'supported_operators': ['+', '-', '=', '?'],
         'values': ["Stream", "Wireless", "TV", "Phono", "Coax1", "Coax2",
                    "Opt1", "Opt2"]
         },
    'Main.Version':
        {'supported_operators': ['?'],
         'type': str
         },
    'Main.Model':
        {'supported_operators': ['?'],
         'values': ['NADC338']
         }
}


class NADReceiverTCPC338(asyncio.Protocol):
    PORT = 30001

    CMD_MIN_INTERVAL = 0.15

    # A connection that dies sooner than this counts as unstable, and the
    # pause before the next reconnect doubles, up to MAX_RECONNECT_INTERVAL.
    STABLE_CONNECTION_TIME = 60
    MAX_RECONNECT_INTERVAL = 300

    def __init__(self, host, loop, state_changed_cb=None,
                 reconnect_interval=15, connect_timeout=10):
        self._loop = loop
        self._host = host
        self._state_changed_cb = state_changed_cb
        self._reconnect_interval = reconnect_interval
        self._connect_timeout = connect_timeout

        self._transport = None
        self._buffer = ''
        self._last_cmd_time = 0

        self._closing = False
        self._state = {}

        self._connect_task = None
        self._connected_at = None
        self._reconnect_delay = reconnect_interval

    @staticmethod
    def make_command(command, operator, value=None):
        cmd_desc = C338_CMDS[command]
        # validate operator
        if operator in cmd_desc['supported_operators']:
            if operator == '=' and value is None:
                raise ValueError("No value provided")
            elif operator in ['?', '-', '+'] and value is not None:
                raise ValueError(
                    "Operator \'%s\' cannot be called with a value" % operator)

            if value is None:
                cmd = command + operator
            else:
                # validate value
                if 'min' in cmd_desc:
                    if not cmd_desc['min'] <= value <= cmd_desc['max']:
                        raise ValueError("Given value \'%s\' is not within %s..%s"
                                         % (value, cmd_desc['min'], cmd_desc['max']))
                elif 'values' in cmd_desc:
                    if 'type' in cmd_desc and cmd_desc['type'] == bool:
                        value = cmd_desc['values'][int(value)]
                    elif value not in cmd_desc['values']:
                        raise ValueError("Given value \'%s\' is not one of %s"
                                         % (value, cmd_desc['values']))

                cmd = command + operator + str(value)
        else:
            raise ValueError("Invalid operator provided %s" % operator)

        return cmd

    @staticmethod
    def parse_part(response):
        key, value = response.split('=', 1)

        cmd_desc = C338_CMDS[key]

        # convert the data to the correct type
        if 'type' in cmd_desc:
            if cmd_desc['type'] == bool:
                value = bool(cmd_desc['values'].index(value))
            else:
                value = cmd_desc['type'](value)

        return key, value

    def connection_made(self, transport):
        self._transport = transport

        self._connected_at = time.monotonic()

        sock = self._transport.get_extra_info('socket')
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_KEEPIDLE, 30)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_KEEPCNT, 3)

        _LOGGER.debug("Connected to %s", self._host)
        self._loop.create_task(self.exec_command('Main', '?'))

    def data_received(self, data):
        data = data.decode('utf-8').replace('\x00', '')

        self._buffer += data

        new_state = {}
        while True:
            ends = [i for i in (self._buffer.find('\r'), self._buffer.find('\n')) if i >= 0]
            if not ends:
                break
            line, self._buffer = self._buffer[:min(ends)], self._buffer[min(ends) + 1:]
            if not line:
                continue
            # An exception here would make asyncio drop the connection, and
            # the reconnect would hit the same line again: skip it instead.
            try:
                key, value = self.parse_part(line)
            except (KeyError, ValueError, IndexError):
                _LOGGER.debug("Ignoring unrecognized line from %s: %r", self._host, line)
                continue
            new_state[key] = value

            # volume changes implicitly disables mute,
            if key == 'Main.Volume' and self._state.get('Main.Mute') is True:
                new_state['Main.Mute'] = False

        if new_state:
            _LOGGER.debug("state changed %s", new_state)
            self._state.update(new_state)
            if self._state_changed_cb:
                self._state_changed_cb(self._state)

    def connection_lost(self, exc):
        if exc:
            _LOGGER.error("Disconnected from %s because of %s", self._host, exc)
        else:
            _LOGGER.debug("Disconnected from %s because of close/abort.",
                          self._host)
        self._transport = None

        self._state.clear()
        if self._state_changed_cb:
            self._state_changed_cb(self._state)

        if self._closing:
            return
        uptime = time.monotonic() - (self._connected_at or 0)
        if uptime >= self.STABLE_CONNECTION_TIME:
            self._reconnect_delay = self._reconnect_interval
        delay = self._reconnect_delay
        # The next drop within STABLE_CONNECTION_TIME waits twice as long.
        self._reconnect_delay = min(delay * 2, self.MAX_RECONNECT_INTERVAL)
        _LOGGER.info("Reconnecting to %s in %ss", self._host, delay)
        self.start(delay=delay)

    def start(self, delay=0):
        """Connect in the background (after `delay` seconds), retrying until connected."""
        self._closing = False
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = self._loop.create_task(self._connect_loop(delay))

    async def connect(self):
        """Connect, retrying until connected or disconnect() is called."""
        self.start()
        await asyncio.shield(self._connect_task)

    async def _connect_loop(self, delay):
        if delay:
            await asyncio.sleep(delay)
        failures = 0
        while not self._closing and not self._transport:
            try:
                _LOGGER.debug("Connecting to %s", self._host)
                connection = self._loop.create_connection(
                    lambda: self, self._host, NADReceiverTCPC338.PORT)
                await asyncio.wait_for(
                    connection, timeout=self._connect_timeout)
                if failures:
                    _LOGGER.info("Connected to %s again", self._host)
                return
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as err:
                failures += 1
                # Only the first failure of an outage is worth a warning.
                log = _LOGGER.warning if failures == 1 else _LOGGER.debug
                log("Error connecting to %s (%s), retrying every %ss",
                    self._host, err or type(err).__name__, self._reconnect_interval)
                await asyncio.sleep(self._reconnect_interval)

    async def disconnect(self):
        self._closing = True
        self._state_changed_cb = None
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        if self._transport:
            self._transport.close()

    async def exec_command(self, command, operator, value=None):
        if self._transport:
            # throttle commands to CMD_MIN_INTERVAL
            cmd_wait_time = (self._last_cmd_time
                             + NADReceiverTCPC338.CMD_MIN_INTERVAL) - time.time()
            if cmd_wait_time > 0:
                await asyncio.sleep(cmd_wait_time)
            cmd = self.make_command(command, operator, value)
            # The NAD protocol terminates every command with a carriage return.
            self._transport.write((cmd + '\r').encode('utf-8'))

            self._last_cmd_time = time.time()

    async def status(self):
        """Return the state of the device."""
        return self._state

    async def power_off(self):
        """Power the device off."""
        await self.exec_command(CMD_POWER, '=', False)

    async def power_on(self):
        """Power the device on."""
        await self.exec_command(CMD_POWER, '=', True)

    async def set_volume(self, volume):
        """Set volume level of the device. Accepts integer values -80-0."""
        await self.exec_command(CMD_VOLUME, '=', float(volume))

    async def volume_down(self):
        await self.exec_command(CMD_VOLUME, '-')

    async def volume_up(self):
        await self.exec_command(CMD_VOLUME, '+')

    async def mute(self):
        """Mute the device."""
        await self.exec_command(CMD_MUTE, '=', True)

    async def unmute(self):
        """Unmute the device."""
        await self.exec_command(CMD_MUTE, '=', False)

    async def select_source(self, source):
        """Select a source from the list of sources."""
        await self.exec_command(CMD_SOURCE, '=', source)

    def available_sources(self):
        """Return a list of available sources."""
        return list(C338_CMDS[CMD_SOURCE]['values'])
