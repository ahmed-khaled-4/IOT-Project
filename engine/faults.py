from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Tuple

from .room import Room


@dataclass
class FaultTickResult:
    dropout_active: bool
    telemetry_delay_sec: Optional[float] = None


class FaultInjector:
    """
    Per-room fault injector.

    Runs in the room task; therefore each instance is isolated and doesn't need locks.
    """

    def __init__(self, *, room_seed: int, config: dict, room_id: str = "", log_fn=None):
        self._rng = random.Random(room_seed)
        self._cfg = config
        self._room_id = room_id
        self._log = log_fn or (lambda msg: None)

        # Sensor drift
        self._drift_bias: float = 0.0
        self._drift_active: bool = False
        self._drift_sign: float = 1.0

        # Frozen sensor
        self._frozen_until_tick: Optional[int] = None
        self._frozen_value: Optional[float] = None
        self._frozen_which_sensor: str = str(
            config.get("sensor_drift", {}).get("which_sensor", "temperature")
        )

        frozen_cfg = config.get("frozen_sensor", {}) or {}
        self._frozen_duration_ticks: int = int(frozen_cfg.get("duration_ticks", 6))
        self._frozen_sensor_which: str = str(frozen_cfg.get("which_sensor", "temperature")).lower()

        # Node dropout (silence)
        self._dropout_until_tick: Optional[int] = None

        # Telemetry delay range
        td_cfg = config.get("telemetry_delay", {}) or {}
        self._delay_min = float(td_cfg.get("min_delay_sec", 1.0))
        self._delay_max = float(td_cfg.get("max_delay_sec", 10.0))

        # Sensor drift config
        drift_cfg = config.get("sensor_drift", {}) or {}
        self._drift_max = float(drift_cfg.get("drift_max", 2.0))
        self._drift_step = float(drift_cfg.get("drift_step", 0.01))

    def _pick_fault_type(self) -> str:
        # In Phase 1 we keep equal probability across the required faults.
        options = ["sensor_drift", "frozen_sensor", "telemetry_delay", "node_dropout"]
        return options[self._rng.randrange(len(options))]

    def _duration_ticks_from_sim(self, tick_dt_sim_sec: float, duration_sec: float) -> int:
        if tick_dt_sim_sec <= 0:
            return max(1, int(duration_sec))
        return max(1, int(duration_sec / tick_dt_sim_sec))

    def tick(self, *, room: Room, tick_index: int, tick_dt_sim_sec: float) -> FaultTickResult:
        """
        Apply sensor faults to the room state (in-place), and decide whether
        telemetry should be delayed or the node should dropout.
        """
        # If faults disabled, no-op.
        if not bool(self._cfg.get("enabled", True)):
            return FaultTickResult(dropout_active=False, telemetry_delay_sec=None)

        # Dropout takes precedence (silence fully during dropout window).
        if self._dropout_until_tick is not None:
            if tick_index < self._dropout_until_tick:
                return FaultTickResult(dropout_active=True, telemetry_delay_sec=None)
            self._dropout_until_tick = None

        # Potentially trigger a new fault at this tick.
        probability = float(self._cfg.get("probability", 0.0))
        telemetry_delay_sec: Optional[float] = None

        if self._rng.random() < probability:
            fault_type = self._pick_fault_type()

            if fault_type == "sensor_drift":
                self._drift_active = True
                if self._drift_bias == 0.0:
                    self._drift_sign = 1.0 if self._rng.random() >= 0.5 else -1.0
                self._log(f"fault.triggered type=sensor_drift room_id={self._room_id} tick={tick_index} bias={self._drift_bias:.3f}")

            elif fault_type == "frozen_sensor":
                self._frozen_until_tick = tick_index + self._frozen_duration_ticks
                if self._frozen_sensor_which == "humidity":
                    self._frozen_value = float(room.humidity)
                else:
                    self._frozen_value = float(room.temperature)
                self._log(f"fault.triggered type=frozen_sensor room_id={self._room_id} tick={tick_index} sensor={self._frozen_sensor_which} frozen_value={self._frozen_value:.2f}")

            elif fault_type == "telemetry_delay":
                telemetry_delay_sec = self._rng.uniform(self._delay_min, self._delay_max)
                self._log(f"fault.triggered type=telemetry_delay room_id={self._room_id} tick={tick_index} delay_sec={telemetry_delay_sec:.2f}")

            elif fault_type == "node_dropout":
                nd_cfg = self._cfg.get("node_dropout", {}) or {}
                duration_sec = float(nd_cfg.get("duration_sec", 30))
                self._dropout_until_tick = tick_index + self._duration_ticks_from_sim(
                    tick_dt_sim_sec=tick_dt_sim_sec, duration_sec=duration_sec
                )
                self._log(f"fault.triggered type=node_dropout room_id={self._room_id} tick={tick_index} until_tick={self._dropout_until_tick}")

        # Apply persistent faults to the current sensor readings (in-place).
        if self._drift_active:
            # Accumulate gradual bias toward the configured max.
            if abs(self._drift_bias) < self._drift_max:
                self._drift_bias += self._drift_sign * self._drift_step

            # Apply bias to temperature reading.
            room.temperature = float(room.temperature + self._drift_bias)

        if self._frozen_until_tick is not None and tick_index < self._frozen_until_tick:
            # Override the frozen sensor reading with the stored value.
            if self._frozen_sensor_which == "humidity":
                if self._frozen_value is not None:
                    room.humidity = float(self._frozen_value)
            else:
                if self._frozen_value is not None:
                    room.temperature = float(self._frozen_value)
        else:
            # If the freeze expired, clear it.
            if self._frozen_until_tick is not None and tick_index >= self._frozen_until_tick:
                self._frozen_until_tick = None
                self._frozen_value = None

        return FaultTickResult(dropout_active=False, telemetry_delay_sec=telemetry_delay_sec)

