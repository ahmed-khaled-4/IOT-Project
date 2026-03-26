# MicroPython firmware for Wokwi reference room.
#
# This is a "single room" POC that:
# - reads temperature/humidity (DHT22)
# - reads occupancy (PIR motion sensor)
# - reads ambient light (photoresistor / LDR)
# - publishes telemetry to MQTT as JSON
# - listens for actuator commands and validates incoming payloads

import machine
import time
import json

from umqtt.simple import MQTTClient
import network

try:
    import dht
except ImportError:
    dht = None


# ---------------- Configuration (edit to match your local MQTT setup) ----------------
MQTT_HOST = "10.0.0.2"
MQTT_PORT = 1883

TOPIC_TELEMETRY = b"campus/bldg_01/floor_01/room_101/telemetry"
TOPIC_HEARTBEAT = b"campus/bldg_01/floor_01/room_101/heartbeat"
TOPIC_COMMAND = b"campus/bldg_01/floor_01/room_101/command"
ROOM_SENSOR_ID = "b01-f01-r101"

# Virtual epoch base (since MicroPython in Wokwi doesn't do NTP by default).
EPOCH_BASE = 1700000000  # fixed start; acts like "fake NTP"

# Publish cadence
TELEMETRY_INTERVAL_SEC = 5
HEARTBEAT_INTERVAL_SEC = 15


# Actuator defaults
HVAC_MODE = "ECO"  # ON / OFF / ECO
TARGET_TEMP = 24.0
LIGHTING_DIMMER = 60


# ---------------- Sensor setup ----------------
DHT_PIN = 15
PIR_PIN = 14
LDR_ADC_PIN = 34  # ADC1 input

pir = machine.Pin(PIR_PIN, machine.Pin.IN)
ldr_adc = machine.ADC(machine.Pin(LDR_ADC_PIN))

if dht is not None:
    dht_sensor = dht.DHT22(machine.Pin(DHT_PIN))
else:
    dht_sensor = None


# ---------------- WiFi (Wokwi network) ----------------
ssid = "wokwi"
password = ""


def connect_wifi():
    # Wokwi provides a "virtual" WiFi; this call keeps structure similar to real ESP32 code.
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        # Wait briefly.
        for _ in range(50):
            if wlan.isconnected():
                break
            time.sleep_ms(100)
    return wlan


def _now_epoch_sec():
    # uptime_ms/1000 approximates simulated time.
    return EPOCH_BASE + int(time.ticks_ms() / 1000)


def read_temperature_humidity():
    if dht_sensor is None:
        return 22.0, 45.0
    dht_sensor.measure()
    return float(dht_sensor.temperature()), float(dht_sensor.humidity())


def read_occupancy():
    return bool(pir.value() == 1)


def read_light_level_lux():
    # Convert ADC reading to a lux-like range 0..1000 deterministically.
    raw = ldr_adc.read()  # 0..4095
    # Assume inverse-ish relationship; scale to 0..1000 for the engine.
    lux = int((raw / 4095) * 1000)
    if lux < 0:
        lux = 0
    if lux > 1000:
        lux = 1000
    return lux


def validate_command_payload(obj):
    # Minimal schema enforcement (MicroPython friendly).
    if not isinstance(obj, dict):
        return False
    allowed = {"hvac_mode", "target_temp", "lighting_dimmer"}
    for k in obj.keys():
        if k not in allowed:
            return False
    if "hvac_mode" in obj:
        if obj["hvac_mode"] not in ("ON", "OFF", "ECO"):
            return False
    if "target_temp" in obj:
        try:
            float(obj["target_temp"])
        except:
            return False
    if "lighting_dimmer" in obj:
        try:
            v = int(obj["lighting_dimmer"])
        except:
            return False
        if v < 0 or v > 100:
            return False
    return True


def apply_command(obj):
    global HVAC_MODE, TARGET_TEMP, LIGHTING_DIMMER
    if "hvac_mode" in obj:
        HVAC_MODE = obj["hvac_mode"]
    if "target_temp" in obj:
        TARGET_TEMP = float(obj["target_temp"])
    if "lighting_dimmer" in obj:
        LIGHTING_DIMMER = int(obj["lighting_dimmer"])


def mqtt_callback(topic, msg):
    try:
        if topic != TOPIC_COMMAND:
            return
        # msg is bytes
        s = msg.decode("utf-8")
        obj = json.loads(s)
    except:
        print("Command invalid JSON")
        return

    if not validate_command_payload(obj):
        print("Command rejected by schema")
        return

    apply_command(obj)


def main():
    connect_wifi()

    client = MQTTClient("wokwi-room-101", MQTT_HOST, MQTT_PORT)
    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(TOPIC_COMMAND)

    last_telemetry = time.ticks_ms()
    last_heartbeat = time.ticks_ms()

    while True:
        # Handle incoming commands without blocking long.
        try:
            client.check_msg()
        except:
            pass

        now_ms = time.ticks_ms()
        if (now_ms - last_telemetry) >= TELEMETRY_INTERVAL_SEC * 1000:
            last_telemetry = now_ms

            temp_c, hum = read_temperature_humidity()
            occupancy = read_occupancy()
            light_level = read_light_level_lux()

            # Lighting correlation: if occupied, keep light above a threshold.
            if occupancy and light_level < 300:
                light_level = 300
                LIGHTING_DIMMER = 80
            elif (not occupancy) and light_level < 120:
                LIGHTING_DIMMER = 40

            payload = {
                "sensor_id": ROOM_SENSOR_ID,
                "timestamp": _now_epoch_sec(),
                "temperature": float(temp_c),
                "humidity": float(hum),
                "occupancy": bool(occupancy),
                "light_level": int(light_level),
                "hvac_mode": str(HVAC_MODE),
                "lighting_dimmer": int(LIGHTING_DIMMER),
            }

            client.publish(TOPIC_TELEMETRY, json.dumps(payload))

        if (now_ms - last_heartbeat) >= HEARTBEAT_INTERVAL_SEC * 1000:
            last_heartbeat = now_ms
            hb = {
                "sensor_id": ROOM_SENSOR_ID,
                "timestamp": _now_epoch_sec(),
                "status": "Healthy",
            }
            client.publish(TOPIC_HEARTBEAT, json.dumps(hb))

        time.sleep_ms(100)


main()

