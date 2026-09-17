Fork of [martonperei/ha-nadtcp](https://github.com/martonperei/ha-nadtcp), which is unmaintained. This fork:

- Replaces the removed `SUPPORT_*`/`DEVICE_CLASS_RECEIVER` constants with modern `MediaPlayerEntityFeature`/`MediaPlayerDeviceClass`.
- Marks the state-change callback with `@callback` so it doesn't get killed by Home Assistant's thread-safety guard.
- Vendors the `nadtcp` PyPI client as `nadtcp_client.py` instead of depending on the unmaintained `nadtcp==0.2.0.dev2` package, which called `asyncio.wait_for()`/`asyncio.sleep()` with a `loop=` kwarg removed in Python 3.10 (this made `connect()` raise immediately and the entity stay `unavailable`).

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
