from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TopicScheme:
    """Centralised topic builder matching the Phase 2 rubric.

    All topics live under `<prefix>/fXX/rYYY/...` where `prefix` is
    `campus/b01` by default.  Zero-padded floor IDs, raw room_code
    (which is already `floor*100 + index`).
    """

    prefix: str = "campus/b01"

    def telemetry(self, floor_id: int, room_code: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/telemetry"

    def heartbeat(self, floor_id: int, room_code: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/heartbeat"

    def command(self, floor_id: int, room_code: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/cmd"

    def response(self, floor_id: int, room_code: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/response"

    def status(self, floor_id: int, room_code: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/status"

    def floor_summary(self, floor_id: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/summary"

    def fleet_health(self) -> str:
        return f"{self.prefix}/fleet/health"

    def fleet_command(self) -> str:
        return f"{self.prefix}/fleet/cmd"

    def room_command_subscription(self) -> str:
        return f"{self.prefix}/+/+/cmd"


_ROOM_TOPIC_RE = re.compile(r"^.*/f(\d{2})/r(\d+)/([a-zA-Z0-9_]+)$")


def parse_room_topic(topic: str) -> Optional[tuple[int, int, str]]:
    """Return (floor_id, room_code, suffix) for a topic like
    `campus/b01/f03/r305/cmd`, or None if the topic is malformed.
    """

    m = _ROOM_TOPIC_RE.match(topic)
    if not m:
        return None
    try:
        floor_id = int(m.group(1))
        room_code = int(m.group(2))
        suffix = m.group(3)
    except ValueError:
        return None
    return floor_id, room_code, suffix


def make_room_id(building_id: int, floor_id: int, room_code: int) -> str:
    return f"b{building_id:02d}-f{floor_id:02d}-r{room_code}"
