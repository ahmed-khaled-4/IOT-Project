from __future__ import annotations

import math
from dataclasses import dataclass

from .environment import Environment
from .room import Room


@dataclass(frozen=True)
class ThermalParams:
    alpha: float       # insulation / thermal leakage constant
    beta: float        # HVAC strength
    occupancy_heat: float
    humidity_gain: float

    def hvac_power(self, hvac_mode: str) -> float:
        mode = str(hvac_mode).upper()
        if mode == "ON":
            return 1.0
        if mode == "ECO":
            return 0.5
        return 0.0


class PhysicsEngine:

    def __init__(
        self,
        env: Environment,
        thermal_params: ThermalParams,
        *,
        light_occupied_threshold: int,
        light_unoccupied_min: int,
        light_unoccupied_max: int,
        light_occupied_min: int,
        light_occupied_max: int,
    ):
        self._env = env
        self._p = thermal_params
        self._light_occupied_threshold = int(light_occupied_threshold)
        self._light_unoccupied_min = int(light_unoccupied_min)
        self._light_unoccupied_max = int(light_unoccupied_max)
        self._light_occupied_min = int(light_occupied_min)
        self._light_occupied_max = int(light_occupied_max)

    @staticmethod
    def _deterministic_hash01(x: float) -> float:
        """Deterministic pseudo-random in [0,1) using fractional sine."""
        v = math.sin(x) * 43758.5453123
        return v - math.floor(v)

    def update_room(self, room: Room, *, tick_dt_sim_sec: float) -> None:
        t_out = self._env.outside_temp_c()
        t_curr = float(room.temperature)

        # Occupancy: correlate with day/night and room identity
        hour = self._env.virtual_hour_of_day()
        daytime = 6.0 <= hour <= 18.0
        phase = self._deterministic_hash01(room.room_code * 0.123 + hour * 2.17)
        room.occupancy = bool(daytime and phase > 0.35)

        # Newton's Law of Cooling: thermal leakage toward outside temp
        leakage = self._p.alpha * (t_out - t_curr) * tick_dt_sim_sec

        # HVAC drives temp toward target
        hvac_power = self._p.hvac_power(room.hvac_mode)
        hvac_delta = self._p.beta * hvac_power * (room.target_temp - t_curr) * tick_dt_sim_sec

        # Occupants add heat
        occupancy_delta = self._p.occupancy_heat * (1.0 if room.occupancy else 0.0) * tick_dt_sim_sec

        t_next = t_curr + leakage + hvac_delta + occupancy_delta
        room.temperature = float(t_next)

        # Humidity correlates with temp changes
        temp_delta = t_next - t_curr
        hum_next = room.humidity + self._p.humidity_gain * temp_delta + (0.2 if room.occupancy else -0.1)
        room.humidity = float(hum_next)

        # Light level correlates with occupancy
        u = self._deterministic_hash01(room.room_code * 1.37 + hour * 3.01)
        if room.occupancy:
            base = self._light_occupied_min + u * (self._light_occupied_max - self._light_occupied_min)
            room.light_level = int(max(self._light_occupied_threshold, base))
            room.lighting_dimmer = int(max(room.lighting_dimmer, 60))
        else:
            base = self._light_unoccupied_min + u * (self._light_unoccupied_max - self._light_unoccupied_min)
            room.light_level = int(base)
            room.lighting_dimmer = int(min(room.lighting_dimmer, 40))
