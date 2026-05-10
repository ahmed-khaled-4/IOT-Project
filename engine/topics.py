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

    # --- Phase 3: OTA topics ---

    def ota_broadcast(self) -> str:
        return f"{self.prefix}/ota/config"

    def ota_floor(self, floor_id: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/ota"

    def ota_room(self, floor_id: int, room_code: int) -> str:
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/ota"

    def ota_subscription_floor(self) -> str:
        """Wildcard subscription for floor-targeted OTA messages."""
        return f"{self.prefix}/+/ota"

    def ota_subscription_room(self) -> str:
        """Wildcard subscription for room-targeted OTA messages."""
        return f"{self.prefix}/+/+/ota"

    def ota_alerts(self) -> str:
        return f"{self.prefix}/ota/alerts"

    # --- Phase 3: Shadow state / client attribute topics ---

    def client_attributes(self, floor_id: int, room_code: int) -> str:
        """Engine publishes reported state + current_version here for tb-gateway to forward as client attributes."""
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/attributes"

    def attr_update(self, floor_id: int, room_code: int) -> str:
        """tb-gateway publishes desired shared attributes to this topic for the engine to consume."""
        return f"{self.prefix}/f{floor_id:02d}/r{room_code}/attr-update"

    def attr_update_subscription(self) -> str:
        """Wildcard subscription to receive all attr-update messages."""
        return f"{self.prefix}/+/+/attr-update"


_ROOM_TOPIC_RE = re.compile(r"^.*/f(\d{2})/r(\d+)/([a-zA-Z0-9_-]+)$")


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
