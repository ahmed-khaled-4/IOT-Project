from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from gmqtt import Client as MQTTClient
from gmqtt import Message as MQTTMessage
from gmqtt.mqtt.constants import MQTTv50
from jsonschema import Draft7Validator

from ..room import Room
from ..topics import TopicScheme, parse_room_topic, make_room_id
from .base import CommandHandler, RoomTransport, TransportKind
from .dup_filter import DupFilter, build_mqtt_key


ROOM_COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "hvac_mode": {"type": "string", "enum": ["ON", "OFF", "ECO"]},
        "target_temp": {"type": "number"},
        "lighting_dimmer": {"type": "integer", "minimum": 0, "maximum": 100},
        "command_id": {"type": "string"},
    },
    "additionalProperties": True,
}


class _RoomMQTTConn:
    """One persistent gmqtt client per MQTT room.

    Publishes its own telemetry/heartbeat under its unique ClientID and
    receives downstream commands on its own ``.../cmd`` topic. The LWT
    is set before connect so HiveMQ auto-publishes ``status=offline`` if
    the TCP session drops ungracefully.
    """

    def __init__(
        self,
        *,
        room: Room,
        building_id: int,
        username: Optional[str],
        password: Optional[str],
        broker_host: str,
        broker_port: int,
        topics: TopicScheme,
        qos_telemetry: int,
        qos_command: int,
        tls_ctx: Optional[ssl.SSLContext],
        validator: Draft7Validator,
        dup_filter: DupFilter,
        on_command: CommandHandler,
        log_fn,
    ):
        self._room = room
        self._building_id = int(building_id)
        self._topics = topics
        self._qos_t = int(qos_telemetry)
        self._qos_c = int(qos_command)
        self._tls_ctx = tls_ctx
        self._validator = validator
        self._dup_filter = dup_filter
        self._on_command = on_command
        self._log = log_fn
        self._broker_host = broker_host
        self._broker_port = int(broker_port)
        self._username = username
        self._password = password
        self._connected = False

        client_id = make_room_id(building_id, room.floor_id, room.room_code)
        lwt_topic = topics.status(room.floor_id, room.room_code)
        lwt_epoch = int(time.time())
        lwt_payload = json.dumps(
            {
                "sensor_id": client_id,
                "state": "offline",
                "reason": "lwt",
                "tb_profile": room.thingsboard_device_profile(),
                "timestamp": lwt_epoch,
                "ts": lwt_epoch * 1000,
                "tb_ts_ms": lwt_epoch * 1000,
            }
        )
        lwt = MQTTMessage(
            lwt_topic,
            lwt_payload,
            qos=1,
            retain=True,
        )

        self._client = MQTTClient(
            client_id=client_id,
            will_message=lwt,
            session_expiry_interval=0,
        )
        if username:
            self._client.set_auth_credentials(username, password or "")

        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    @property
    def client_id(self) -> str:
        return make_room_id(self._building_id, self._room.floor_id, self._room.room_code)

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        kwargs: Dict[str, Any] = {}
        if self._tls_ctx is not None:
            kwargs["ssl"] = self._tls_ctx
        await self._client.connect(self._broker_host, self._broker_port, **kwargs)

    async def disconnect(self) -> None:
        try:
            online = {
                "sensor_id": self.client_id,
                "state": "offline",
                "reason": "graceful",
                "tb_profile": self._room.thingsboard_device_profile(),
                "timestamp": int(time.time()),
                "ts": int(time.time() * 1000),
            }
            self._client.publish(
                self._topics.status(self._room.floor_id, self._room.room_code),
                json.dumps(online),
                qos=1,
                retain=True,
            )
        except Exception:
            pass
        try:
            await self._client.disconnect()
        except Exception:
            pass
        self._connected = False

    def publish_telemetry(self, payload: Dict[str, Any]) -> None:
        topic = self._topics.telemetry(self._room.floor_id, self._room.room_code)
        self._client.publish(topic, json.dumps(payload), qos=self._qos_t)

    def publish_heartbeat(self, payload: Dict[str, Any]) -> None:
        topic = self._topics.heartbeat(self._room.floor_id, self._room.room_code)
        self._client.publish(topic, json.dumps(payload), qos=self._qos_t)

    def publish_response(self, payload: Dict[str, Any]) -> None:
        topic = self._topics.response(self._room.floor_id, self._room.room_code)
        self._client.publish(topic, json.dumps(payload), qos=self._qos_c)

    def publish_status(self, status: str) -> None:
        payload = {
            "sensor_id": self.client_id,
            "state": status,
            "tb_profile": self._room.thingsboard_device_profile(),
            "timestamp": int(time.time()),
            "ts": int(time.time() * 1000),
            "tb_ts_ms": int(time.time() * 1000),
        }
        topic = self._topics.status(self._room.floor_id, self._room.room_code)
        self._client.publish(topic, json.dumps(payload), qos=1, retain=True)

    def _handle_connect(self, client, flags, rc, properties) -> None:
        self._connected = True
        cmd_topic = self._topics.command(self._room.floor_id, self._room.room_code)
        client.subscribe(cmd_topic, qos=self._qos_c)
        online = {
            "sensor_id": self.client_id,
            "state": "online",
            "tb_profile": self._room.thingsboard_device_profile(),
            "timestamp": int(time.time()),
            "ts": int(time.time() * 1000),
            "tb_ts_ms": int(time.time() * 1000),
        }
        client.publish(
            self._topics.status(self._room.floor_id, self._room.room_code),
            json.dumps(online),
            qos=1,
            retain=True,
        )
        self._log(f"mqtt.node.connected room_id={self.client_id} rc={rc}")

    def _handle_disconnect(self, client, packet, exc=None) -> None:
        self._connected = False
        self._log(f"mqtt.node.disconnected room_id={self.client_id} exc={exc}")

    def _handle_message(self, client, topic, payload, qos, properties) -> None:
        try:
            topic_str = str(topic)
            payload_str = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
            data = json.loads(payload_str)
        except Exception as e:
            self._log(f"mqtt.command.invalid_json topic={topic_str} err={type(e).__name__}:{e}")
            return

        # Dedup using packet id when available, payload hash otherwise
        packet_id = None
        try:
            packet_id = properties.get("packet_id") if isinstance(properties, dict) else None
        except Exception:
            packet_id = None

        dup_flag = False
        try:
            dup_flag = bool(properties.get("dup")) if isinstance(properties, dict) else False
        except Exception:
            dup_flag = False

        key = build_mqtt_key(topic_str, packet_id, hash(payload_str))
        if self._dup_filter.seen(key):
            self._log(
                f"mqtt.command.dedup_drop room_id={self.client_id} topic={topic_str} "
                f"dup_flag={dup_flag} packet_id={packet_id}"
            )
            return

        errors = sorted(self._validator.iter_errors(data), key=lambda e: e.path)
        if errors:
            self._log(f"mqtt.command.invalid_schema room_id={self.client_id} errors={len(errors)}")
            return

        parsed = parse_room_topic(topic_str)
        if parsed is None:
            self._log(f"mqtt.command.unrecognized_topic topic={topic_str}")
            return
        floor_id, room_code, suffix = parsed
        room_id = make_room_id(self._building_id, floor_id, room_code)
        maybe = self._on_command(room_id, data)
        if asyncio.iscoroutine(maybe):
            asyncio.create_task(maybe)


class MQTTTransport(RoomTransport):
    """Aggregate transport that manages N per-room gmqtt clients."""

    kind = TransportKind.MQTT

    def __init__(
        self,
        *,
        rooms: Iterable[Room],
        building_id: int,
        broker_host: str,
        broker_port: int,
        topics: TopicScheme,
        qos_telemetry: int = 1,
        qos_command: int = 2,
        tls_ctx: Optional[ssl.SSLContext] = None,
        credentials: Optional[Dict[str, tuple[str, str]]] = None,
        dup_filter: Optional[DupFilter] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self._rooms = list(rooms)
        self._building_id = int(building_id)
        self._broker_host = broker_host
        self._broker_port = int(broker_port)
        self._topics = topics
        self._qos_t = int(qos_telemetry)
        self._qos_c = int(qos_command)
        self._tls_ctx = tls_ctx
        self._credentials = credentials or {}
        self._dup_filter = dup_filter or DupFilter()
        self._log = log_fn
        self._validator = Draft7Validator(ROOM_COMMAND_SCHEMA)

        self._handler: Optional[CommandHandler] = None
        self._conns: Dict[str, _RoomMQTTConn] = {}
        self._fleet_client: Optional[MQTTClient] = None  # for fleet.health + fleet.cmd

    @property
    def connected(self) -> bool:
        if not self._conns:
            return False
        return all(c.connected for c in self._conns.values())

    def register_command_handler(self, handler: CommandHandler) -> None:
        self._handler = handler

    def _ensure_handler(self) -> CommandHandler:
        if self._handler is None:
            raise RuntimeError("MQTTTransport: command handler not registered")
        return self._handler

    async def start(self) -> None:
        handler = self._ensure_handler()
        # Build and connect all per-room clients concurrently
        for room in self._rooms:
            room_id = make_room_id(self._building_id, room.floor_id, room.room_code)
            user, pw = self._credentials.get(room_id, (None, None))
            conn = _RoomMQTTConn(
                room=room,
                building_id=self._building_id,
                username=user,
                password=pw,
                broker_host=self._broker_host,
                broker_port=self._broker_port,
                topics=self._topics,
                qos_telemetry=self._qos_t,
                qos_command=self._qos_c,
                tls_ctx=self._tls_ctx,
                validator=self._validator,
                dup_filter=self._dup_filter,
                on_command=handler,
                log_fn=self._log,
            )
            self._conns[room_id] = conn

        connect_tasks = [asyncio.create_task(c.connect()) for c in self._conns.values()]
        for t in connect_tasks:
            try:
                await t
            except Exception as e:
                self._log(f"mqtt.node.connect_failed err={type(e).__name__}:{e}")

        # Also open a fleet-scope client for fleet.health publications and
        # fleet.cmd subscription.
        self._fleet_client = MQTTClient(client_id="world-engine-fleet")
        user, pw = self._credentials.get("fleet", (None, None))
        if user:
            self._fleet_client.set_auth_credentials(user, pw or "")

        def _on_fleet_msg(client, topic, payload, qos, properties):
            try:
                data = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload))
            except Exception:
                return
            errors = sorted(self._validator.iter_errors(data), key=lambda e: e.path)
            if errors:
                self._log(f"mqtt.fleet_cmd.invalid_schema errors={len(errors)}")
                return
            maybe = handler("__fleet__", data)
            if asyncio.iscoroutine(maybe):
                asyncio.create_task(maybe)

        def _on_fleet_connect(client, flags, rc, properties):
            client.subscribe(self._topics.fleet_command(), qos=self._qos_c)
            self._log(f"mqtt.fleet.connected rc={rc}")

        self._fleet_client.on_connect = _on_fleet_connect
        self._fleet_client.on_message = _on_fleet_msg
        try:
            kwargs: Dict[str, Any] = {}
            if self._tls_ctx is not None:
                kwargs["ssl"] = self._tls_ctx
            await self._fleet_client.connect(self._broker_host, self._broker_port, **kwargs)
        except Exception as e:
            self._log(f"mqtt.fleet.connect_failed err={type(e).__name__}:{e}")
            self._fleet_client = None

    async def stop(self) -> None:
        disconnects: List[asyncio.Task] = []
        for c in self._conns.values():
            disconnects.append(asyncio.create_task(c.disconnect()))
        for t in disconnects:
            try:
                await t
            except Exception:
                pass
        if self._fleet_client is not None:
            try:
                await self._fleet_client.disconnect()
            except Exception:
                pass

    def _get_conn(self, room: Any) -> Optional[_RoomMQTTConn]:
        room_id = make_room_id(self._building_id, room.floor_id, room.room_code)
        return self._conns.get(room_id)

    def publish_telemetry(self, room: Any, payload: Dict[str, Any]) -> None:
        c = self._get_conn(room)
        if c is not None:
            c.publish_telemetry(payload)

    def publish_heartbeat(self, room: Any) -> None:
        c = self._get_conn(room)
        if c is None:
            return
        payload = {
            "sensor_id": c.client_id,
            "timestamp": int(room.last_update),
            "status": "Healthy",
            "tb_profile": room.thingsboard_device_profile(),
            "ts": int(room.last_update) * 1000,
            "tb_ts_ms": int(room.last_update) * 1000,
        }
        c.publish_heartbeat(payload)

    def publish_response(self, room: Any, payload: Dict[str, Any]) -> None:
        c = self._get_conn(room)
        if c is not None:
            c.publish_response(payload)

    def publish_status(self, room: Any, status: str) -> None:
        c = self._get_conn(room)
        if c is not None:
            c.publish_status(status)

    def publish_fleet_health(self, payload: Dict[str, Any]) -> None:
        if self._fleet_client is None:
            return
        self._fleet_client.publish(self._topics.fleet_health(), json.dumps(payload), qos=1)
