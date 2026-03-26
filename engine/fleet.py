from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .room import Room


@dataclass
class FleetCommand:
    target_temp: Optional[float] = None
    hvac_mode: Optional[str] = None
    lighting_dimmer: Optional[int] = None

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "FleetCommand":
        return cls(
            target_temp=payload.get("target_temp"),
            hvac_mode=payload.get("hvac_mode"),
            lighting_dimmer=payload.get("lighting_dimmer"),
        )


class FleetManager:
    """
    Fleet orchestration:
    - instantiates 200 rooms (building/floor/room hierarchy)
    - routes fleet-wide & room-specific actuator commands to per-room queues
    - monitors heartbeat and publishes fleet health
    """

    def __init__(
        self,
        *,
        buildings: int,
        floors_per_building: int,
        rooms_per_floor: int,
        clock,
        persistence,
        publish_health_fn: Callable[[str, Dict[str, Any]], None],
        health_topic: str,
        heartbeat_timeout_sec: float,
        heartbeat_check_interval_sec: float,
        log_fn,
        defaults: dict,
        rooms_total_expected: int,
        rooms_limit: int | None = None,
    ):
        self._buildings = int(buildings)
        self._floors_per_building = int(floors_per_building)
        self._rooms_per_floor = int(rooms_per_floor)
        self._clock = clock
        self._persistence = persistence
        self._publish_health_fn = publish_health_fn
        self._health_topic = health_topic
        self._heartbeat_timeout_sec = float(heartbeat_timeout_sec)
        self._heartbeat_check_interval_sec = float(heartbeat_check_interval_sec)
        self._log = log_fn
        self._rooms_total_expected = int(rooms_total_expected)
        self._rooms_limit = rooms_limit if rooms_limit is None else int(rooms_limit)

        self.rooms_by_id: Dict[str, Room] = {}
        self.command_queues: Dict[str, asyncio.Queue[Dict[str, Any]]] = {}
        self._heartbeat_monotonic: Dict[str, float] = {}
        self._heartbeat_lock = asyncio.Lock()

        self._initialize_rooms(defaults=defaults)

    def _initialize_rooms(self, *, defaults: dict) -> None:
        created = 0
        for b in range(1, self._buildings + 1):
            for f in range(1, self._floors_per_building + 1):
                for r_in_floor in range(1, self._rooms_per_floor + 1):
                    if self._rooms_limit is not None and created >= self._rooms_limit:
                        return
                    room_code = f * 100 + r_in_floor
                    room_id = Room.make_room_id(b, f, room_code)
                    room = Room.create_default(
                        building_id=b,
                        floor_id=f,
                        room_code=room_code,
                        default_temp=defaults["default_temp"],
                        default_humidity=defaults["default_humidity"],
                        default_hvac_mode=defaults["default_hvac_mode"],
                        default_target_temp=defaults["default_target_temp"],
                        lighting_dimmer_default=defaults["lighting_dimmer_default"],
                    )
                    self.rooms_by_id[room_id] = room
                    self.command_queues[room_id] = asyncio.Queue()
                    created += 1

        if len(self.rooms_by_id) != self._rooms_total_expected:
            self._log(
                f"fleet.init_rooms size_mismatch actual={len(self.rooms_by_id)} expected={self._rooms_total_expected}"
            )

    def update_heartbeat(self, room_id: str) -> None:
        """
        Called by room tasks when they publish heartbeat.
        Uses monotonic timestamps to avoid issues with clock jumps.
        """
        # Dict assignment is atomic under CPython's GIL; monitor takes a snapshot under lock.
        self._heartbeat_monotonic[room_id] = time.monotonic()

    def request_room_command(self, room_id: str, payload: Dict[str, Any]) -> None:
        q = self.command_queues.get(room_id)
        if not q:
            self._log(f"fleet.command.unknown_room room_id={room_id}")
            return
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            self._log(f"fleet.command.queue_full room_id={room_id}")

    def request_fleet_command(self, payload: Dict[str, Any]) -> None:
        for room_id, q in self.command_queues.items():
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self._log(f"fleet.command.queue_full room_id={room_id}")

    async def heartbeat_monitor_task(self, stop_event: asyncio.Event) -> None:
        """
        Monitor heartbeat freshness and publish fleet health.
        """
        # Publish cadence: roughly aligned with monitor checks.
        while not stop_event.is_set():
            now = time.monotonic()

            # Timeout is configured in SIMULATED seconds. Convert to real monotonic by dividing acceleration.
            sim_acc = float(getattr(self._clock, "time_acceleration", 1.0) or 1.0)
            timeout_real = self._heartbeat_timeout_sec / sim_acc

            async with self._heartbeat_lock:
                snapshot = dict(self._heartbeat_monotonic)

            healthy = 0
            warning = 0

            silent_rooms = []
            for room_id in self.rooms_by_id.keys():
                last = snapshot.get(room_id)
                if last is None or (now - last) > timeout_real:
                    warning += 1
                    silent_rooms.append(room_id)
                else:
                    healthy += 1

            # Structured log.
            if warning > 0:
                self._log(
                    json.dumps(
                        {
                            "event": "fleet.health.warning",
                            "healthy": healthy,
                            "warning": warning,
                            "silent_rooms_sample": silent_rooms[:5],
                            "timestamp_monotonic": now,
                        }
                    )
                )

            health_payload = {
                "fleet_id": "campus",
                "healthy_rooms": healthy,
                "warning_rooms": warning,
                "silent_rooms_count": warning,
                "timestamp": int(self._clock.virtual_epoch_sec()),
            }
            try:
                self._publish_health_fn(self._health_topic, health_payload)
            except Exception as e:
                self._log(f"fleet.health.publish_error error={type(e).__name__}:{e}")

            await asyncio.sleep(self._heartbeat_check_interval_sec)

