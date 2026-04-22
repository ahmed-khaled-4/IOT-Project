"""Backwards-compatibility shim for the legacy single-broker MQTT client.

Phase 2 moves per-room MQTT handling into
:mod:`engine.transport.mqtt_transport`.  This module is kept as a thin
re-export so any external tooling that imports ``engine.mqtt_client``
continues to work.
"""

from __future__ import annotations

from .transport.mqtt_transport import MQTTTransport, ROOM_COMMAND_SCHEMA

MQTTClientWrapper = MQTTTransport  # alias for legacy callers

__all__ = ["MQTTTransport", "MQTTClientWrapper", "ROOM_COMMAND_SCHEMA"]
