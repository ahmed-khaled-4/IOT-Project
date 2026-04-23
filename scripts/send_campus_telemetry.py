#!/usr/bin/env python3
"""Publish one test telemetry message to HiveMQ (Phase 2 campus topic layout).

Same JSON shape as the world engine. Use to verify a room in ThingsBoard
(Campus-HiveMQ-Gateway + tb-gateway must be up).

Examples:

  # MQTT room f01 r101
  python scripts/send_campus_telemetry.py b01-f01-r101

  # CoAP room f01 r111
  python scripts/send_campus_telemetry.py b01-f01-r111

  # Custom broker (compose maps host 1885 -> 1883 in the broker)
  python scripts/send_campus_telemetry.py b01-f02-r105 --host 127.0.0.1 --port 1885
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Install paho-mqtt: pip install paho-mqtt", file=sys.stderr)
    sys.exit(1)

_RE_ROOM = re.compile(r"^b(\d+)-f(\d+)-r(\d+)$")


def _profile_for_room_code(room_code: int) -> str:
    r = room_code % 100
    if 11 <= r <= 20:
        return "CoAP-ThermalSensor"
    return "MQTT-ThermalSensor"


def _build_payload(
    room_id: str, floor: int, room_code: int, args: argparse.Namespace
) -> dict:
    now = int(time.time())
    t_ms = now * 1000
    return {
        "sensor_id": room_id,
        "tb_profile": _profile_for_room_code(room_code),
        "temperature": float(args.temperature),
        "humidity": float(args.humidity),
        "occupancy": bool(args.occupancy),
        "light_level": int(args.light_level),
        "hvac_mode": str(args.hvac_mode),
        "lighting_dimmer": int(args.lighting_dimmer),
        "timestamp": now,
        "ts": t_ms,
        "tb_ts_ms": t_ms,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "room_id",
        help="Room id, e.g. b01-f01-r101 (MQTT) or b01-f01-r111 (CoAP)",
    )
    p.add_argument("--host", default="127.0.0.1", help="HiveMQ host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=1885, help="HiveMQ port on host (default: 1885)")
    p.add_argument("-u", "--user", default="thingsboard", help="MQTT user")
    p.add_argument("-P", "--password", default="changeit", help="MQTT password")
    p.add_argument("-q", "--qos", type=int, default=1, choices=(0, 1, 2))
    p.add_argument("--temperature", type=float, default=24.0)
    p.add_argument("--humidity", type=float, default=45.0)
    p.add_argument("--occupancy", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--light-level", type=int, default=400, dest="light_level")
    p.add_argument("--hvac-mode", default="ECO", dest="hvac_mode")
    p.add_argument("--lighting-dimmer", type=int, default=60, dest="lighting_dimmer")
    p.add_argument("--dry-run", action="store_true", help="Print topic + JSON only, do not publish")
    args = p.parse_args()

    m = _RE_ROOM.match(args.room_id.strip())
    if not m:
        p.error("room_id must look like b01-f01-r101 (b#-f#-r#).")
    building, floor, room_code = int(m.group(1)), int(m.group(2)), int(m.group(3))
    f_s = f"{floor:02d}"
    r_s = str(room_code)
    topic = f"campus/b{building:02d}/f{f_s}/r{r_s}/telemetry"
    room_id = f"b{building:02d}-f{f_s}-r{r_s}"
    if room_id != args.room_id.strip():
        p.error(f"normalized room id {room_id!r} != input (check format)")

    body = _build_payload(room_id, floor, room_code, args)
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if args.dry_run:
        print("Topic:", topic)
        print("Payload:", raw.decode("utf-8"))
        return

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="send_campus_telemetry")
    client.username_pw_set(args.user, args.password)
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()
    pr = client.publish(topic, raw, qos=args.qos, retain=False)
    pr.wait_for_publish()
    client.loop_stop()
    client.disconnect()
    print(f"Published {args.qos} to {args.host}:{args.port} {topic} ({room_id} profile {body['tb_profile']})")


if __name__ == "__main__":
    main()
