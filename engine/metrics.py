from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import psutil


@dataclass
class TickStats:
    tick_count: int = 0
    total_processing_ms: float = 0.0
    max_processing_ms: float = 0.0

    def record_processing(self, processing_ms: float) -> None:
        self.tick_count += 1
        self.total_processing_ms += float(processing_ms)
        if processing_ms > self.max_processing_ms:
            self.max_processing_ms = float(processing_ms)

    @property
    def avg_processing_ms(self) -> float:
        if self.tick_count <= 0:
            return 0.0
        return self.total_processing_ms / self.tick_count


class MetricsCollector:
    """
    Lightweight metrics collection suitable for high-concurrency asyncio workloads.
    """

    def __init__(
        self,
        *,
        latency_threshold_ms: float,
        summary_interval_sec: float = 60.0,
        process: Optional[psutil.Process] = None,
        log_fn=print,
    ):
        self._latency_threshold_ms = float(latency_threshold_ms)
        self._summary_interval_sec = float(summary_interval_sec)
        self._log = log_fn
        self._proc = process or psutil.Process(os.getpid())

        self._tick_stats = TickStats()
        self._loop_latency_stats = TickStats()

        self._stop_event: Optional[asyncio.Event] = None

    def record_tick_processing(self, processing_ms: float) -> None:
        self._tick_stats.record_processing(processing_ms)

    async def run_event_loop_latency_monitor(self, *, stop_event: asyncio.Event, sleep_sec: float = 0.2) -> None:
        """
        Measures "event loop latency" as the overshoot of a periodic asyncio wakeup.
        """
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            start = loop.time()
            await asyncio.sleep(sleep_sec)
            elapsed = loop.time() - start
            overshoot = max(0.0, elapsed - sleep_sec)

            self._loop_latency_stats.record_processing(overshoot * 1000.0)
            if (overshoot * 1000.0) > self._latency_threshold_ms:
                self._log(
                    f"metrics.event_loop_latency_threshold exceeded overshoot_ms={overshoot*1000.0:.2f}"
                )

    async def run_summary_task(self, *, stop_event: asyncio.Event) -> None:
        """
        Periodically prints summary metrics and resets counters.
        """
        while not stop_event.is_set():
            await asyncio.sleep(self._summary_interval_sec)

            # Snapshot and reset.
            tick_count = self._tick_stats.tick_count
            avg_proc = self._tick_stats.avg_processing_ms
            max_proc = self._tick_stats.max_processing_ms

            loop_count = self._loop_latency_stats.tick_count
            avg_loop = self._loop_latency_stats.avg_processing_ms
            max_loop = self._loop_latency_stats.max_processing_ms

            cpu_percent = self._proc.cpu_percent(interval=None)
            mem_mb = self._proc.memory_info().rss / (1024.0 * 1024.0)

            self._log(
                f"metrics.summary tick_count={tick_count} avg_tick_ms={avg_proc:.2f} max_tick_ms={max_proc:.2f} "
                f"loop_avg_ms={avg_loop:.2f} loop_max_ms={max_loop:.2f} cpu_percent={cpu_percent:.1f} mem_mb={mem_mb:.1f}"
            )

            # Reset by recreating objects (safe under GIL for this Phase).
            self._tick_stats = TickStats()
            self._loop_latency_stats = TickStats()

