"""Support for NAD digital amplifiers which can be remote controlled via tcp/ip."""
import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.core import callback
from homeassistant.components.media_player import (
    MediaPlayerEntity, PLATFORM_SCHEMA, MediaPlayerDeviceClass,
    MediaPlayerEntityFeature)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN, STATE_UNAVAILABLE,
    EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP)

from .nadtcp_client import (
    NADReceiverTCPC338, CMD_POWER, CMD_VOLUME, CMD_MUTE, CMD_SOURCE)

_LOGGER = logging.getLogger(__name__)

DEFAULT_RECONNECT_INTERVAL = 10
DEFAULT_NAME = 'NAD amplifier'
DEFAULT_MIN_VOLUME = -80
DEFAULT_MAX_VOLUME = -10
DEFAULT_VOLUME_STEP = 4

SUPPORT_NAD = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.SELECT_SOURCE
)

CONF_MIN_VOLUME = 'min_volume'
CONF_MAX_VOLUME = 'max_volume'
CONF_VOLUME_STEP = 'volume_step'
CONF_RECONNECT_INTERVAL = 'reconnect_interval'
CONF_HOST = 'host'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_RECONNECT_INTERVAL, default=DEFAULT_RECONNECT_INTERVAL): int,
    vol.Optional(CONF_MIN_VOLUME, default=DEFAULT_MIN_VOLUME): int,
    vol.Optional(CONF_MAX_VOLUME, default=DEFAULT_MAX_VOLUME): int,
    vol.Optional(CONF_VOLUME_STEP, default=DEFAULT_VOLUME_STEP): int,
})


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Setup the NAD platform."""
    async_add_entities([NADEntity(
        config.get(CONF_NAME),
        config.get(CONF_HOST),
        config.get(CONF_RECONNECT_INTERVAL),
        config.get(CONF_MIN_VOLUME),
        config.get(CONF_MAX_VOLUME),
        config.get(CONF_VOLUME_STEP),
    )])

    return True


class NADEntity(MediaPlayerEntity):
    """Entity handler for the NAD protocol"""

    def __init__(self, name, host, reconnect_interval, min_volume, max_volume, volume_step):
        """Initialize the entity properties"""
        self._client = None
        self._unsub_start = None
        self._unsub_stop = None
        self._name = name
        self._host = host
        self._reconnect_interval = reconnect_interval
        self._min_vol = min_volume
        self._max_vol = max_volume
        self._volume_step = volume_step

        self._state = STATE_UNKNOWN
        self._muted = None
        self._volume = None
        self._source = None

        self._attr_unique_id = f"nadtcp2_{host}"

    def nad_vol_to_internal_vol(self, nad_vol):
        """Convert the configured volume range to internal volume range.
        Takes into account configured min and max volume.
        """
        if nad_vol is None:
            volume_internal = 0.0
        elif nad_vol < self._min_vol:
            volume_internal = 0.0
        elif nad_vol > self._max_vol:
            volume_internal = 1.0
        else:
            volume_internal = (nad_vol - self._min_vol) / \
                              (self._max_vol - self._min_vol)
        return volume_internal

    def internal_vol_to_nad_vol(self, internal_vol):
        return int(round(internal_vol * (self._max_vol - self._min_vol) + self._min_vol))

    def _clamp(self, nad_vol):
        """Keep a volume within the configured min/max range."""
        return max(self._min_vol, min(self._max_vol, nad_vol))

    async def _step_volume(self, direction):
        if self._volume is None:
            _LOGGER.debug("Volume is not known yet, ignoring volume step")
            return
        current = self.internal_vol_to_nad_vol(self._volume)
        # volume_step counts half dB; move at least one whole dB per step.
        delta = max(1, int(self._volume_step * 0.5 + 0.5))
        await self._client.set_volume(self._clamp(current + direction * delta))

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def device_class(self):
        """Return the class of this device."""
        return MediaPlayerDeviceClass.RECEIVER

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def icon(self):
        """Return the icon for the device."""
        return "mdi:speaker-multiple"

    @property
    def source(self):
        """Name of the current input source."""
        return self._source

    @property
    def source_list(self):
        """List of available input sources."""
        if self._client is None:
            return []
        return self._client.available_sources()

    @property
    def available(self):
        """Return if device is available."""
        return self._state != STATE_UNKNOWN

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._volume

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        return self._muted

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_NAD

    async def async_turn_off(self):
        """Turn the media player off."""
        await self._client.power_off()

    async def async_turn_on(self):
        """Turn the media player on."""
        await self._client.power_on()

    async def async_volume_up(self):
        """Step volume up in the configured increments."""
        await self._step_volume(1)

    async def async_volume_down(self):
        """Step volume down in the configured increments."""
        await self._step_volume(-1)

    async def async_set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        await self._client.set_volume(self._clamp(self.internal_vol_to_nad_vol(volume)))

    async def async_mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""
        if mute:
            await self._client.mute()
        else:
            await self._client.unmute()

    async def async_select_source(self, source):
        """Select input source."""
        await self._client.select_source(source)

    @callback
    def _handle_state_changed(self, state):
        """Apply a state update from the client (called in the event loop)."""
        if CMD_POWER in state:
            self._state = STATE_ON if state[CMD_POWER] else STATE_OFF
        else:
            self._state = STATE_UNKNOWN

        if CMD_VOLUME in state:
            self._volume = self.nad_vol_to_internal_vol(state[CMD_VOLUME])
        if CMD_MUTE in state:
            self._muted = state[CMD_MUTE]
        if CMD_SOURCE in state:
            self._source = state[CMD_SOURCE]

        if self.hass is not None:
            self.async_write_ha_state()

    async def async_added_to_hass(self):
        self._client = NADReceiverTCPC338(self._host, self.hass.loop,
                                          reconnect_interval=self._reconnect_interval,
                                          state_changed_cb=self._handle_state_changed)

        async def on_stop(event):
            self._unsub_stop = None
            await self._client.disconnect()

        # Registered right away, so the connection is closed on shutdown even
        # if it was never established.
        self._unsub_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

        # Connect in the background: an unreachable amplifier must not block
        # the platform setup.
        @callback
        def on_start(event):
            self._unsub_start = None
            self._client.start()

        if self.hass.is_running:
            self._client.start()
        else:
            self._unsub_start = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START, on_start)

    async def async_will_remove_from_hass(self):
        """Close the connection so a removed or reloaded entity frees its slot."""
        # One-shot listeners that already fired must not be removed again.
        for unsub in (self._unsub_start, self._unsub_stop):
            if unsub is not None:
                unsub()
        self._unsub_start = self._unsub_stop = None
        if self._client is not None:
            await self._client.disconnect()
