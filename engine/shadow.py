"""Phase 3: Shadow State Manager (Desired vs Reported).

The "shadow" pattern tracks two versions of each room's actuator state:
- Desired: set by an administrator via ThingsBoard Shared Attributes.
- Reported: the actual state of the room as last confirmed by the engine.

When a mismatch is detected (desired != reported), the ShadowManager pushes
the desired state into the room's command queue on the next heartbeat cycle.
Only after the engine processes the command and publishes updated client
attributes does the reported state clear the mismatch.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from .room import Room, HVAC_MODES


class ShadowManager:
    """Per-room shadow state: desired vs reported reconciliation."""

    def __init__(
        self,
        *,
        command_queues: Dict[str, asyncio.Queue],
        log_fn: Callable[[str], None],
        desired_hvac_key: str = "desired_hvac_mode",
        desired_dimmer_key: str = "desired_lighting_dimmer",
    ):
        self._queues = command_queues
        self._log = log_fn
        self._desired_hvac_key = desired_hvac_key
        self._desired_dimmer_key = desired_dimmer_key

        # room_id → {"desired_hvac_mode": str, "desired_lighting_dimmer": int}
        self._desired: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Ingress: called by MQTTTransport when attr-update arrives from TB
    # ------------------------------------------------------------------

    def on_desired_received(self, room_id: str, payload: Dict[str, Any]) -> None:
        """Store the desired state received from ThingsBoard Shared Attributes."""
        desired = self._desired.setdefault(room_id, {})
        updated_keys = []

        if self._desired_hvac_key in payload:
            val = str(payload[self._desired_hvac_key]).upper()
            if val in HVAC_MODES:
                desired["desired_hvac_mode"] = val
                updated_keys.append(f"hvac_mode={val}")
            else:
                self._log(f"shadow.invalid_hvac room_id={room_id} value={val!r}")

        if self._desired_dimmer_key in payload:
            try:
                val = int(payload[self._desired_dimmer_key])
                val = max(0, min(100, val))
                desired["desired_lighting_dimmer"] = val
                updated_keys.append(f"dimmer={val}")
            except (TypeError, ValueError):
                self._log(f"shadow.invalid_dimmer room_id={room_id} raw={payload.get(self._desired_dimmer_key)!r}")

        if updated_keys:
            self._log(f"shadow.desired_updated room_id={room_id} keys={','.join(updated_keys)}")

    # ------------------------------------------------------------------
    # Reconciliation: called after each heartbeat publish
    # ------------------------------------------------------------------

    def is_out_of_sync(self, room: Room) -> bool:
        """Return True if the desired state differs from the room's current (reported) state."""
        desired = self._desired.get(room.room_id)
        if not desired:
            return False
        if "desired_hvac_mode" in desired and desired["desired_hvac_mode"] != room.hvac_mode:
            return True
        if "desired_lighting_dimmer" in desired and desired["desired_lighting_dimmer"] != room.lighting_dimmer:
            return True
        return False

    def apply_desired(self, room: Room) -> bool:
        """Push the desired state into the room's command queue if out of sync.

        Returns True if a reconciliation command was queued.
        """
        if not self.is_out_of_sync(room):
            return False

        desired = self._desired.get(room.room_id, {})
        cmd: Dict[str, Any] = {}

        if "desired_hvac_mode" in desired and desired["desired_hvac_mode"] != room.hvac_mode:
            cmd["hvac_mode"] = desired["desired_hvac_mode"]
        if "desired_lighting_dimmer" in desired and desired["desired_lighting_dimmer"] != room.lighting_dimmer:
            cmd["lighting_dimmer"] = desired["desired_lighting_dimmer"]

        if not cmd:
            return False

        q = self._queues.get(room.room_id)
        if q is None:
            self._log(f"shadow.no_queue room_id={room.room_id}")
            return False

        try:
            q.put_nowait(cmd)
            self._log(
                f"shadow.reconcile_queued room_id={room.room_id} "
                f"desired_hvac={desired.get('desired_hvac_mode')!r} "
                f"desired_dimmer={desired.get('desired_lighting_dimmer')!r} "
                f"reported_hvac={room.hvac_mode!r} "
                f"reported_dimmer={room.lighting_dimmer!r}"
            )
            return True
        except asyncio.QueueFull:
            self._log(f"shadow.queue_full room_id={room.room_id} — reconcile skipped")
            return False

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def sync_status(self, room: Room) -> str:
        """Return 'IN_SYNC' or 'OUT_OF_SYNC' for dashboard/logs."""
        return "OUT_OF_SYNC" if self.is_out_of_sync(room) else "IN_SYNC"

    def desired_state(self, room_id: str) -> Dict[str, Any]:
        """Return a copy of the desired state for the given room."""
        return dict(self._desired.get(room_id, {}))

    def all_out_of_sync(self) -> list[str]:
        """Return a list of room_ids currently out of sync (for diagnostics)."""
        from .room import Room as _Room
        return list(self._desired.keys())  # caller must call is_out_of_sync per room
