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
    building_id: int
    floor_id: int
    room_code: int
    room_id: str

    temperature: float
    humidity: float
    occupancy: bool
    light_level: int

    hvac_mode: str
    target_temp: float
    lighting_dimmer: int

    last_update: int

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
        if "hvac_mode" in payload:
            mode = str(payload["hvac_mode"]).upper()
            if mode in HVAC_MODES:
                self.hvac_mode = mode
        if "target_temp" in payload:
            self.target_temp = float(payload["target_temp"])
        if "lighting_dimmer" in payload:
            self.lighting_dimmer = int(payload["lighting_dimmer"])
        self.last_update = now_unix_sec()

    def thingsboard_device_profile(self) -> str:
        """Device profile name in ThingsBoard (matches bootstrap MQTT vs CoAP split)."""
        idx = self.room_code % 100
        if 11 <= idx <= 20:
            return "CoAP-ThermalSensor"
        return "MQTT-ThermalSensor"

    def validate_and_clamp(self) -> None:
        self.temperature = clamp(self.temperature, 15.0, 50.0)
        self.humidity = clamp(self.humidity, 0.0, 100.0)
        self.light_level = int(clamp(float(self.light_level), 0.0, 1000.0))
        self.lighting_dimmer = int(clamp(float(self.lighting_dimmer), 0.0, 100.0))
        if self.hvac_mode not in HVAC_MODES:
            self.hvac_mode = "ECO"
        self.target_temp = clamp(self.target_temp, 15.0, 50.0)

    def to_telemetry_json(self, *, timestamp: int) -> Dict[str, Any]:
        self.validate_and_clamp()
        return {
            "sensor_id": self.room_id,
            "timestamp": int(timestamp),
            # IoT Gateway 3.7 JSON converter uses data["ts"] else data["timestamp"] for *all* keys (ignores tsField).
            # ThingsBoard expects Unix ms — set `ts` explicitly; keep seconds in `timestamp` for campus tooling.
            "ts": int(timestamp) * 1000,
            "tb_ts_ms": int(timestamp) * 1000,
            "tb_profile": self.thingsboard_device_profile(),
            # Two decimals: matches what operators type in manual MQTT tests; physics still uses full float internally.
            "temperature": round(float(self.temperature), 2),
            "humidity": round(float(self.humidity), 2),
            "occupancy": bool(self.occupancy),
            "light_level": int(self.light_level),
            "hvac_mode": str(self.hvac_mode),
            "lighting_dimmer": int(self.lighting_dimmer),
        }

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "Room":
        room_id = str(row["room_id"])
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
