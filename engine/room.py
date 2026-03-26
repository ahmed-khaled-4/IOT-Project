from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


HVAC_MODES = {"ON", "OFF", "ECO"}


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def now_unix_sec() -> int:
    return int(time.time())


@dataclass
class Room:
    # Identity
    building_id: int
    floor_id: int
    room_code: int  # e.g. 502 for floor 5, room 02
    room_id: str  # e.g. b01-f05-r502

    # State
    temperature: float
    humidity: float
    occupancy: bool
    light_level: int  # lux (0-1000)

    hvac_mode: str  # ON/OFF/ECO
    target_temp: float
    lighting_dimmer: int  # 0-100 (%)

    last_update: int  # unix seconds

    # Runtime-only bookkeeping (not persisted)
    _frozen_until: Optional[int] = field(default=None, repr=False)
    _last_heartbeat_published: int = field(default=0, repr=False)

    @staticmethod
    def make_room_id(building_id: int, floor_id: int, room_code: int) -> str:
        return f"b{building_id:02d}-f{floor_id:02d}-r{room_code}"

    @classmethod
    def create_default(
        cls,
        *,
        building_id: int,
        floor_id: int,
        room_code: int,
        default_temp: float,
        default_humidity: float,
        default_hvac_mode: str,
        default_target_temp: float,
        lighting_dimmer_default: int,
    ) -> "Room":
        room_id = cls.make_room_id(building_id, floor_id, room_code)
        return cls(
            building_id=building_id,
            floor_id=floor_id,
            room_code=room_code,
            room_id=room_id,
            temperature=float(default_temp),
            humidity=float(default_humidity),
            occupancy=False,
            light_level=0,
            hvac_mode=default_hvac_mode,
            target_temp=float(default_target_temp),
            lighting_dimmer=int(lighting_dimmer_default),
            last_update=now_unix_sec(),
        )

    def apply_command(self, payload: Dict[str, Any]) -> None:
        """
        Apply backend/dashboard command(s) to actuator targets.

        Expected fields (all optional):
        - hvac_mode: ON/OFF/ECO
        - target_temp: float
        - lighting_dimmer: int (0-100)
        """
        if "hvac_mode" in payload:
            mode = str(payload["hvac_mode"]).upper()
            if mode in HVAC_MODES:
                self.hvac_mode = mode
        if "target_temp" in payload:
            self.target_temp = float(payload["target_temp"])
        if "lighting_dimmer" in payload:
            self.lighting_dimmer = int(payload["lighting_dimmer"])

        self.last_update = now_unix_sec()

    def validate_and_clamp(self) -> None:
        # Spec ranges from the course handout.
        self.temperature = clamp(self.temperature, 15.0, 50.0)
        self.humidity = clamp(self.humidity, 0.0, 100.0)
        self.light_level = int(clamp(float(self.light_level), 0.0, 1000.0))
        self.lighting_dimmer = int(clamp(float(self.lighting_dimmer), 0.0, 100.0))

        if self.hvac_mode not in HVAC_MODES:
            # Fail-safe: don't crash the whole simulation if state becomes invalid.
            self.hvac_mode = "ECO"

        self.target_temp = clamp(self.target_temp, 15.0, 50.0)

    def to_telemetry_json(self, *, timestamp: int) -> Dict[str, Any]:
        self.validate_and_clamp()
        return {
            "sensor_id": self.room_id,
            "timestamp": int(timestamp),
            "temperature": float(self.temperature),
            "humidity": float(self.humidity),
            "occupancy": bool(self.occupancy),
            "light_level": int(self.light_level),  # lux
            "hvac_mode": str(self.hvac_mode),
            "lighting_dimmer": int(self.lighting_dimmer),  # 0-100%
        }

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "Room":
        """
        Create a Room from a DB row.

        Table columns are expected to match `engine/persistence.py`.
        """
        # room_id is already a slug like b01-f05-r502
        room_id = str(row["room_id"])

        # Parse identity from slug: bXX-fYY-rZZZ
        try:
            building_part, floor_part, room_part = room_id.split("-")
            building_id = int(building_part.replace("b", ""))
            floor_id = int(floor_part.replace("f", ""))
            room_code = int(room_part.replace("r", ""))
        except Exception:
            raise ValueError(f"Invalid room_id in DB: {room_id}")

        return cls(
            building_id=building_id,
            floor_id=floor_id,
            room_code=room_code,
            room_id=room_id,
            temperature=float(row["last_temp"]),
            humidity=float(row["last_humidity"]),
            occupancy=bool(int(row.get("occupancy", 0))),
            light_level=int(row.get("light_level", 0)),
            hvac_mode=str(row["hvac_mode"]),
            target_temp=float(row["target_temp"]),
            lighting_dimmer=int(row.get("lighting_dimmer", 60)),
            last_update=int(row["last_update"]),
        )

    def db_tuple(self) -> Tuple[Any, ...]:
        """
        Tuple in the same order as the INSERT/UPDATE statement in persistence.
        """
        return (
            self.room_id,
            float(self.temperature),
            float(self.humidity),
            str(self.hvac_mode),
            float(self.target_temp),
            int(self.lighting_dimmer),
            int(1 if self.occupancy else 0),
            int(self.light_level),
            int(self.last_update),
        )

