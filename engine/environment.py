from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass


@dataclass
class VirtualClock:
    """
    Virtual time controller.

    We use a monotonic timer for scheduling accuracy and convert to a simulated
    epoch time for timestamps and environmental effects (outside temperature).
    """

    start_real_epoch_sec: float
    start_monotonic: float
    time_acceleration: float

    def virtual_epoch_sec(self) -> float:
        elapsed_real_sec = time.monotonic() - self.start_monotonic
        return self.start_real_epoch_sec + elapsed_real_sec * self.time_acceleration

    def virtual_hour_of_day(self) -> float:
        # 0..24 hour, based on the virtual epoch time.
        t = self.virtual_epoch_sec()
        hour = (t / 3600.0) % 24.0
        return hour

    def is_daytime(self) -> bool:
        hour = self.virtual_hour_of_day()
        # A simple day window; deterministic and smooth.
        return 6.0 <= hour <= 18.0


class Environment:
    def __init__(self, clock: VirtualClock, *, outside_base: float, outside_amplitude: float):
        self._clock = clock
        self._outside_base = float(outside_base)
        self._outside_amplitude = float(outside_amplitude)

    def outside_temp_c(self) -> float:
        """
        Sinusoidal outside temperature model.

        Uses the handout formula:
        T_outside = base + amplitude * sin(2*pi*(hour - 6)/24)
        """
        hour = self._clock.virtual_hour_of_day()
        return self._outside_base + self._outside_amplitude * math.sin(
            2.0 * math.pi * (hour - 6.0) / 24.0
        )

    @property
    def virtual_time_acceleration(self) -> float:
        return self._clock.time_acceleration

    def virtual_epoch_now_sec(self) -> int:
        return int(self._clock.virtual_epoch_sec())

    def virtual_hour_of_day(self) -> float:
        return self._clock.virtual_hour_of_day()

