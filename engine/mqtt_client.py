from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

from gmqtt import Client as MQTTClient
from jsonschema import Draft7Validator


ROOM_COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "hvac_mode": {"type": "string", "enum": ["ON", "OFF", "ECO"]},
        "target_temp": {"type": "number"},
        "lighting_dimmer": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "additionalProperties": False,
}


def _build_validator(schema: dict) -> Draft7Validator:
    return Draft7Validator(schema)


class MQTTClientWrapper:

    def __init__(
        self,
        *,
        client_id: str,
        broker_host: str,
        broker_port: int,
        qos: int,
        fleet_command_topic: str,
        room_command_subscription: str,
        room_telemetry_topic_builder: Callable[[Any], str],
        room_heartbeat_topic_builder: Callable[[Any], str],
        log_fn,
        on_room_command: Callable[[str, dict], None],
        on_fleet_command: Callable[[dict], None],
    ):
        self._broker_host = broker_host
        self._broker_port = int(broker_port)
        self._qos = int(qos)
        self._fleet_command_topic = fleet_command_topic
        self._room_command_subscription = room_command_subscription
        self._client = MQTTClient(client_id)
        self._log = log_fn
        self._room_telemetry_topic_builder = room_telemetry_topic_builder
        self._room_heartbeat_topic_builder = room_heartbeat_topic_builder
        self._on_room_command = on_room_command
        self._on_fleet_command = on_fleet_command
        self._validator = _build_validator(ROOM_COMMAND_SCHEMA)

        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message

    async def connect_and_subscribe(self) -> None:
        await self._client.connect(self._broker_host, self._broker_port)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def publish(self, topic: str, payload: str, *, qos: Optional[int] = None) -> None:
        self._client.publish(topic, payload, qos or self._qos)

    def publish_telemetry(self, room: Any, payload: dict) -> None:
        topic = self._room_telemetry_topic_builder(room)
        self.publish(topic, json.dumps(payload), qos=self._qos)

    def publish_heartbeat(self, room: Any) -> None:
        topic = self._room_heartbeat_topic_builder(room)
        heartbeat_payload = {
            "sensor_id": room.room_id,
            "timestamp": int(room.last_update),
            "status": "Healthy",
        }
        self.publish(topic, json.dumps(heartbeat_payload), qos=self._qos)

    def _handle_connect(self, client, flags, rc, properties) -> None:
        self._log(f"mqtt.connected rc={rc}")
        client.subscribe(self._fleet_command_topic, qos=self._qos)
        client.subscribe(self._room_command_subscription, qos=self._qos)

    def _handle_message(self, client, topic, payload, qos, properties) -> None:
        try:
            topic_str = str(topic)
            payload_str = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
            data = json.loads(payload_str)
        except Exception as e:
            self._log(f"mqtt.command.invalid_json topic={topic} error={type(e).__name__}:{e}")
            return

        errors = sorted(self._validator.iter_errors(data), key=lambda e: e.path)
        if errors:
            self._log(f"mqtt.command.invalid_schema topic={topic} errors={len(errors)}")
            return

        if topic_str == self._fleet_command_topic:
            self._on_fleet_command(data)
            return

        # Parse room ID from topic: campus/bldg_01/floor_05/room_502/command
        try:
            parts = topic_str.split("/")
            building_id = int(parts[1].replace("bldg_", ""))
            floor_id = int(parts[2].replace("floor_", ""))
            room_code = int(parts[3].replace("room_", ""))
            room_id = f"b{building_id:02d}-f{floor_id:02d}-r{room_code}"
        except Exception:
            self._log(f"mqtt.command.unrecognized_topic topic={topic_str}")
            return

        self._on_room_command(room_id, data)
