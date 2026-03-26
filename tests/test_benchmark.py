import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, Set

from gmqtt import Client as MQTTClient


def _extract_room_code(sensor_id: str) -> str:
    # b01-f05-r502 => room code 502
    m = re.search(r"-r(\d+)$", sensor_id)
    if not m:
        return sensor_id
    return m.group(1)


class BenchmarkMQTTCollector:
    def __init__(self, *, log_prefix: str, log_fn=print):
        self._log_prefix = log_prefix
        self._log_fn = log_fn

        self.seen_telemetry_rooms: Set[str] = set()
        self.seen_heartbeat_rooms: Set[str] = set()

    def attach(self, client: MQTTClient) -> None:
        def on_connect(client, flags, rc, properties):
            # telemetry
            client.subscribe("campus/bldg_01/+/+/telemetry", qos=0)
            # heartbeat
            client.subscribe("campus/bldg_01/+/+/heartbeat", qos=0)

        def on_message(client, topic, payload, qos, properties):
            try:
                t = str(topic)
                s = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
                obj = json.loads(s)
                sensor_id = obj.get("sensor_id")
                if not sensor_id:
                    return
                if t.endswith("/telemetry"):
                    self.seen_telemetry_rooms.add(sensor_id)
                elif t.endswith("/heartbeat"):
                    self.seen_heartbeat_rooms.add(sensor_id)
            except Exception:
                # Benchmark should be robust; ignore malformed messages.
                pass

        client.on_connect = on_connect
        client.on_message = on_message

    async def run_collection(self, *, broker_host: str, broker_port: int, duration_sec: float) -> None:
        client = MQTTClient("benchmark-collector")
        self.attach(client)
        await client.connect(broker_host, broker_port)
        try:
            await asyncio.sleep(duration_sec)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def main_async(args):
    expected_rooms = int(args.rooms)
    duration_sec = float(args.duration_sec)
    mqtt_host = args.mqtt_host
    mqtt_port = int(args.mqtt_port)

    collector = BenchmarkMQTTCollector(log_prefix="bench")

    # Start engine subprocess.
    env = os.environ.copy()
    env.setdefault("MQTT_HOST", mqtt_host)
    env.setdefault("MQTT_PORT", str(mqtt_port))
    env.setdefault("MQTT_TOPIC_PREFIX", "campus/bldg_01")

    cmd = [
        sys.executable,
        "-m",
        "engine.main",
        "--rooms",
        str(expected_rooms),
        "--duration-sec",
        str(duration_sec),
    ]

    # Ensure we run from project root so module imports work.
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    engine_proc = subprocess.Popen(cmd, cwd=project_root, env=env)

    # Run collector concurrently.
    try:
        await collector.run_collection(
            broker_host=mqtt_host,
            broker_port=mqtt_port,
            duration_sec=duration_sec + 10.0,
        )
    finally:
        engine_proc.terminate()
        try:
            engine_proc.wait(timeout=10)
        except Exception:
            engine_proc.kill()

    telemetry_ok = len(collector.seen_telemetry_rooms)
    heartbeat_ok = len(collector.seen_heartbeat_rooms)

    print(
        f"[benchmark] expected_rooms={expected_rooms} telemetry_rooms_seen={telemetry_ok} heartbeat_rooms_seen={heartbeat_ok}"
    )

    # Soft assertions (Phase 1 includes faults and node dropouts).
    if telemetry_ok < expected_rooms * 0.9:
        raise SystemExit(
            f"Telemetry coverage too low: {telemetry_ok}/{expected_rooms} rooms."
        )
    if heartbeat_ok < expected_rooms * 0.85:
        raise SystemExit(
            f"Heartbeat coverage too low: {heartbeat_ok}/{expected_rooms} rooms."
        )


def parse_args():
    p = argparse.ArgumentParser(description="Phase 1 benchmark runner")
    p.add_argument("--mqtt-host", type=str, default="localhost")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--rooms", type=int, default=200)
    p.add_argument("--duration-sec", type=int, default=1800, help="Engine run duration (sec)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main_async(args))

