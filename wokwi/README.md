## Wokwi Reference Room (Phase 1)

This folder contains a MicroPython ESP32 “reference room” that publishes a single-room telemetry JSON to MQTT and listens for actuator commands.

### Files

- `main.py`: MicroPython firmware (DHT22 + PIR + LDR + MQTT)
- `diagram.json`: Wokwi wiring (ESP32 + DHT22 + PIR + LDR)

### How to run (local Wokwi)

1. Open Wokwi (your browser) and create a new project from files.
2. Add `diagram.json` and `main.py`.
3. Edit `wokwi/main.py`:
   - Set `MQTT_HOST` to the IP reachable from Wokwi to your broker (for Phase 1 we run Mosquitto via Docker).
   - Topics are already aligned with the engine defaults for room `b01-f01-r101`.

### Expected MQTT messages

Telemetry:
`campus/bldg_01/floor_01/room_101/telemetry`

Heartbeat:
`campus/bldg_01/floor_01/room_101/heartbeat`

Commands (actuator control):
`campus/bldg_01/floor_01/room_101/command`

