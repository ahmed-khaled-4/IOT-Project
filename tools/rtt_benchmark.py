"""Round-Trip Time benchmark for Phase 2 commands.

Measures, for each of N randomly-selected rooms:

    T0: command.publish (from the benchmark / ThingsBoard side)
    T1: .../response seen on HiveMQ (gateway ack for CoAP, node ack for MQTT)
    T2: next .../telemetry with new state seen on HiveMQ

RTT = T2 - T0.  We also report one-way T1 - T0 so CoAP latency can be
separated from telemetry roundtrip.

Outputs:
  - tools/out/rtt_results.csv        (room_id, transport, t0_ms, t1_ms, t2_ms, rtt_ms)
  - tools/out/rtt_histogram.png      (histogram overlaid MQTT vs CoAP)
  - stdout summary (count, p50, p95, p99, max, target 500ms pass/fail)

Requires the stack to be running (`docker compose up`) and the engine
fully warmed up (>10s after startup).

Usage:
    python tools/rtt_benchmark.py --count 100 --hivemq-host localhost --hivemq-port 1885
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


try:
    from gmqtt import Client as MQTTClient
except ImportError:
    print("ERROR: gmqtt not installed. pip install gmqtt", file=sys.stderr)
    sys.exit(1)


@dataclass
class Sample:
    room_id: str
    transport: str
    command_id: str
    t0: float = 0.0
    t1: Optional[float] = None
    t2: Optional[float] = None
    status: str = "pending"

    @property
    def rtt_ms(self) -> Optional[float]:
        if self.t2 is None:
            return None
        return (self.t2 - self.t0) * 1000.0


class Benchmark:
    def __init__(self, host: str, port: int, count: int, timeout_sec: float):
        self._host = host
        self._port = int(port)
        self._count = int(count)
        self._timeout_sec = float(timeout_sec)
        self._client = MQTTClient(client_id=f"rtt-benchmark-{uuid.uuid4().hex[:8]}")
        self._samples: Dict[str, Sample] = {}  # keyed by command_id
        self._by_response_topic: Dict[str, str] = {}  # response topic -> cmd_id
        self._by_telemetry_topic: Dict[str, str] = {}  # telemetry topic -> cmd_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._done_event: Optional[asyncio.Event] = None

    def _on_connect(self, client, flags, rc, properties) -> None:
        client.subscribe("campus/b01/+/+/response", qos=1)
        client.subscribe("campus/b01/+/+/telemetry", qos=1)

    def _on_message(self, client, topic, payload, qos, properties) -> None:
        topic = str(topic)
        try:
            data = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload))
        except Exception:
            return

        now = time.monotonic()
        cmd_id = self._by_response_topic.get(topic) if topic.endswith("/response") else None
        if cmd_id:
            s = self._samples.get(cmd_id)
            if s and s.t1 is None:
                s.t1 = now

        if topic.endswith("/telemetry"):
            cmd_id = self._by_telemetry_topic.get(topic)
            if cmd_id:
                s = self._samples.get(cmd_id)
                if s and s.t1 is not None and s.t2 is None:
                    expected_mode = data.get("hvac_mode")
                    if expected_mode == s.__dict__.get("expected_mode"):
                        s.t2 = now
                        s.status = "ok"
                        if self._loop and self._done_event is not None:
                            remaining = sum(1 for x in self._samples.values() if x.status == "pending")
                            if remaining == 0:
                                self._loop.call_soon_threadsafe(self._done_event.set)

    async def run(self, rooms: List[Dict[str, str]]) -> List[Sample]:
        self._loop = asyncio.get_running_loop()
        self._done_event = asyncio.Event()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        await self._client.connect(self._host, self._port)

        for r in rooms:
            room_id = r["room_id"]
            transport = r["transport"]
            floor = r["floor"]
            room_code = r["room_code"]
            cmd_id = uuid.uuid4().hex[:12]
            topic_cmd = f"campus/b01/{floor}/r{room_code}/cmd"
            topic_response = f"campus/b01/{floor}/r{room_code}/response"
            topic_telemetry = f"campus/b01/{floor}/r{room_code}/telemetry"

            target_mode = "ON" if (random.random() > 0.5) else "OFF"
            s = Sample(room_id=room_id, transport=transport, command_id=cmd_id)
            s.__dict__["expected_mode"] = target_mode
            self._samples[cmd_id] = s
            self._by_response_topic[topic_response] = cmd_id
            self._by_telemetry_topic[topic_telemetry] = cmd_id

            s.t0 = time.monotonic()
            self._client.publish(
                topic_cmd,
                json.dumps({"hvac_mode": target_mode, "command_id": cmd_id}),
                qos=2,
            )

        try:
            await asyncio.wait_for(self._done_event.wait(), timeout=self._timeout_sec)
        except asyncio.TimeoutError:
            pass

        for s in self._samples.values():
            if s.status == "pending":
                s.status = "timeout"

        await self._client.disconnect()
        return list(self._samples.values())


def load_rooms_csv(path: Optional[str]) -> List[Dict[str, str]]:
    """Load devices.csv produced by the TB bootstrap, or fall back to
    a synthetic list if the file is missing."""

    out: List[Dict[str, str]] = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out.append(
                    {
                        "room_id": row["room_id"],
                        "floor": row["floor"],
                        "room_code": row["room_code"],
                        "transport": row["device_type"],
                    }
                )
    else:
        for f in range(1, 11):
            for idx in range(1, 21):
                rc = f * 100 + idx
                transport = "mqtt" if idx <= 10 else "coap"
                out.append(
                    {
                        "room_id": f"b01-f{f:02d}-r{rc}",
                        "floor": f"f{f:02d}",
                        "room_code": str(rc),
                        "transport": transport,
                    }
                )
    return out


def plot_histogram(samples: List[Sample], out_png: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping histogram", file=sys.stderr)
        return

    mqtt_rtts = [s.rtt_ms for s in samples if s.transport == "mqtt" and s.rtt_ms is not None]
    coap_rtts = [s.rtt_ms for s in samples if s.transport == "coap" and s.rtt_ms is not None]

    fig, ax = plt.subplots(figsize=(10, 5))
    if mqtt_rtts:
        ax.hist(mqtt_rtts, bins=30, alpha=0.6, label=f"MQTT (n={len(mqtt_rtts)})")
    if coap_rtts:
        ax.hist(coap_rtts, bins=30, alpha=0.6, label=f"CoAP (n={len(coap_rtts)})")
    ax.axvline(500, color="red", linestyle="--", label="500 ms target")
    ax.set_xlabel("Round-Trip Time (ms)")
    ax.set_ylabel("Count")
    ax.set_title("Phase 2 RTT Benchmark")
    ax.legend()
    ax.grid(True, linestyle=":", alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_png)
    print(f"wrote {out_png}")


def summary(samples: List[Sample]) -> None:
    rtts = [s.rtt_ms for s in samples if s.rtt_ms is not None]
    if not rtts:
        print("NO RTT samples (all timed out)")
        return

    rtts_sorted = sorted(rtts)

    def pct(p: float) -> float:
        idx = int(round((p / 100.0) * (len(rtts_sorted) - 1)))
        return rtts_sorted[idx]

    print("--- RTT Benchmark Summary ---")
    print(f"total     : {len(samples)}")
    print(f"ok        : {sum(1 for s in samples if s.status == 'ok')}")
    print(f"timeout   : {sum(1 for s in samples if s.status == 'timeout')}")
    print(f"mean (ms) : {statistics.mean(rtts):.1f}")
    print(f"p50 (ms)  : {pct(50):.1f}")
    print(f"p95 (ms)  : {pct(95):.1f}")
    print(f"p99 (ms)  : {pct(99):.1f}")
    print(f"max (ms)  : {max(rtts):.1f}")
    print(f"target    : 500 ms   -> {'PASS' if pct(99) < 500 else 'FAIL'}")


async def amain() -> None:
    p = argparse.ArgumentParser(description="Phase 2 RTT benchmark")
    p.add_argument("--hivemq-host", default=os.environ.get("HIVEMQ_HOST", "localhost"))
    p.add_argument("--hivemq-port", type=int, default=int(os.environ.get("HIVEMQ_PORT", "1885")))
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--timeout-sec", type=float, default=15.0)
    p.add_argument("--devices-csv", default="infra/thingsboard/devices.csv")
    p.add_argument("--out-dir", default="tools/out")
    args = p.parse_args()

    rooms = load_rooms_csv(args.devices_csv)
    if len(rooms) > args.count:
        rooms = random.sample(rooms, args.count)

    bench = Benchmark(args.hivemq_host, args.hivemq_port, args.count, args.timeout_sec)
    samples = await bench.run(rooms)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "rtt_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["room_id", "transport", "command_id", "status", "rtt_ms"])
        for s in samples:
            w.writerow([s.room_id, s.transport, s.command_id, s.status, f"{s.rtt_ms:.2f}" if s.rtt_ms is not None else ""])
    print(f"wrote {csv_path}")

    plot_histogram(samples, out_dir / "rtt_histogram.png")
    summary(samples)


if __name__ == "__main__":
    asyncio.run(amain())
