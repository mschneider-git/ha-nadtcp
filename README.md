Fork of [martonperei/ha-nadtcp](https://github.com/martonperei/ha-nadtcp), which is unmaintained. This fork:

- Replaces the removed `SUPPORT_*`/`DEVICE_CLASS_RECEIVER` constants with modern `MediaPlayerEntityFeature`/`MediaPlayerDeviceClass`.
- Marks the state-change callback with `@callback` so it doesn't get killed by Home Assistant's thread-safety guard.
- Vendors the `nadtcp` PyPI client as `nadtcp_client.py` instead of depending on the unmaintained `nadtcp==0.2.0.dev2` package, which called `asyncio.wait_for()`/`asyncio.sleep()` with a `loop=` kwarg removed in Python 3.10 (this made `connect()` raise immediately and the entity stay `unavailable`).
- Protects the amplifier's connection slots. The C338 firmware does not free the slots of closed connections, so a burst of reconnects locks it up until it is power-cycled:
  - Lines the client does not recognize are skipped. Before, any unknown key or unparseable value made asyncio drop the connection, and the immediate reconnect hit the same line again, reconnecting several times per second.
  - Every reconnect waits `reconnect_interval`. The pause doubles, up to 5 minutes, while connections keep dying within a minute.
  - The connection is closed when the entity is removed or reloaded, and on shutdown even if it was never established.
- Connects in the background, so an unreachable amplifier no longer blocks Home Assistant's startup.
- Volume steps stay within `min_volume`/`max_volume`, whole-dB steps work for any `volume_step`, and 0 dB is accepted.
- Terminates commands with `\r` as the NAD protocol specifies, and sends TCP keepalives every 30 s instead of every second.
- Gives the entity a unique ID, so it can be renamed and customized in the UI.

# nadtcp

configuration.yaml
```yaml
media_player:
  - platform: nadtcp2
    name: nad-amp
    max_volume: -20
    min_volume: -70
    volume_step: 2
    host: 192.168.1.112
```
