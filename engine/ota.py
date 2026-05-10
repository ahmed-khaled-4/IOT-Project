"""Phase 3: OTA (Over-the-Air) firmware/parameter update manager.

Responsibilities:
- Receive OTA payloads from MQTT (broadcast, floor-targeted, or room-targeted).
- Verify SHA-256 signature before applying any update.
- Hot-swap PhysicsEngine ThermalParams on successful verification.
- Bump room.current_version on every successfully applied room.
- Publish a Security Tampering Alert on hash mismatch.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import Any, Callable, Dict, Iterable, Optional

from .physics import PhysicsEngine, ThermalParams
from .room import Room


_FLOOR_TOPIC_RE = re.compile(r".*/f(\d{2})/ota$")
_ROOM_TOPIC_RE = re.compile(r".*/f(\d{2})/r(\d+)/ota$")
_BROADCAST_TOPIC_RE = re.compile(r".*/ota/config$")


def _compute_signature(params: Dict[str, Any]) -> str:
    """SHA-256 over canonical JSON (sort_keys=True) of the params dict only."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OTAManager:
    """Manages OTA updates for the World Engine.

    Thread/asyncio safety: all methods are called from the asyncio event loop;
    the physics engine swap is a single attribute assignment which is atomic
    in CPython due to the GIL.
    """

    def __init__(
        self,
        *,
        physics: PhysicsEngine,
        rooms_by_id: Dict[str, Room],
        log_fn: Callable[[str], None],
        publish_alert_fn: Callable[[Dict[str, Any]], None],
        prefix: str = "campus/b01",
    ):
        self._physics = physics
        self._rooms = rooms_by_id
        self._log = log_fn
        self._publish_alert = publish_alert_fn
        self._prefix = prefix
        self._update_count = 0

    def process_message(self, topic: str, raw_payload: bytes) -> None:
        """Entry point called by MQTTTransport when an OTA topic message arrives."""
        try:
            payload_str = raw_payload.decode("utf-8") if isinstance(raw_payload, (bytes, bytearray)) else str(raw_payload)
            data = json.loads(payload_str)
        except Exception as e:
            self._log(f"ota.parse_error topic={topic} err={type(e).__name__}:{e}")
            return

        if not isinstance(data, dict):
            self._log(f"ota.invalid_payload topic={topic} reason=not_a_dict")
            return

        version = str(data.get("version", "unknown"))
        params = data.get("params", {})
        signature = data.get("signature", "")

        if not params or not isinstance(params, dict):
            self._log(f"ota.invalid_payload topic={topic} reason=missing_params version={version}")
            return

        # Cryptographic verification
        expected = _compute_signature(params)
        if expected != signature:
            self._log(
                f"ota.tamper_detected topic={topic} version={version} "
                f"expected_sig={expected[:16]}... received_sig={str(signature)[:16]}..."
            )
            self._publish_alert({
                "ota_tamper": True,
                "ota_topic": topic,
                "ota_version": version,
                "ota_received_sig": str(signature)[:64],
                "ota_expected_sig": expected[:64],
                "alert_type": "Security Tampering Alert",
                "severity": "CRITICAL",
            })
            return

        # Determine target rooms from topic
        target_rooms = self._resolve_target_rooms(topic, data.get("target", "all"))
        if not target_rooms:
            self._log(f"ota.no_target_rooms topic={topic} version={version}")
            return

        # Build new ThermalParams from incoming params (preserve existing values for unspecified keys)
        current_p = self._physics._p
        new_p = ThermalParams(
            alpha=float(params.get("alpha", current_p.alpha)),
            beta=float(params.get("beta", current_p.beta)),
            occupancy_heat=float(params.get("occupancy_heat", current_p.occupancy_heat)),
            humidity_gain=float(params.get("humidity_gain", current_p.humidity_gain)),
            default_humidity=float(params.get("default_humidity", current_p.default_humidity)),
            humidity_pull=float(params.get("humidity_pull", current_p.humidity_pull)),
        )

        # Hot-swap physics params (CPython GIL makes this safe)
        self._physics._p = new_p

        # Update version on each targeted room
        applied = 0
        for room in target_rooms:
            room.current_version = version
            applied += 1

        self._update_count += 1
        self._log(
            f"ota.applied version={version} rooms={applied} "
            f"alpha={new_p.alpha} beta={new_p.beta} update_seq={self._update_count}"
        )

    def _resolve_target_rooms(self, topic: str, target_hint: Any) -> list[Room]:
        """Return the list of rooms this OTA message applies to based on topic and target field."""
        rooms = self._rooms

        # Room-targeted: campus/b01/fNN/rXXX/ota
        m_room = _ROOM_TOPIC_RE.search(topic)
        if m_room:
            floor_id = int(m_room.group(1))
            room_code = int(m_room.group(2))
            # room_id format: b01-fNN-rXXX
            room_id = f"b01-f{floor_id:02d}-r{room_code}"
            r = rooms.get(room_id)
            return [r] if r is not None else []

        # Floor-targeted: campus/b01/fNN/ota
        m_floor = _FLOOR_TOPIC_RE.search(topic)
        if m_floor:
            floor_id = int(m_floor.group(1))
            return [r for r in rooms.values() if r.floor_id == floor_id]

        # Broadcast: campus/b01/ota/config or target field says "all"
        if _BROADCAST_TOPIC_RE.search(topic):
            return list(rooms.values())

        # Fallback: use the target field from the payload
        if isinstance(target_hint, str):
            if target_hint == "all":
                return list(rooms.values())
            if target_hint.startswith("f") and len(target_hint) == 3:
                try:
                    floor_id = int(target_hint[1:])
                    return [r for r in rooms.values() if r.floor_id == floor_id]
                except ValueError:
                    pass
            # Try as room_id directly
            r = rooms.get(target_hint)
            if r is not None:
                return [r]

        return list(rooms.values())

    @property
    def update_count(self) -> int:
        return self._update_count
