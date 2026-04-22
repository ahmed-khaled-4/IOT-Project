from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

import aiocoap
import aiocoap.resource as resource
from aiocoap import Code, Message
from aiocoap.numbers.contentformat import ContentFormat
from aiocoap.numbers.types import Type

from ..room import Room
from ..topics import TopicScheme, make_room_id
from .base import CommandHandler, RoomTransport, TransportKind
from .dup_filter import DupFilter, build_coap_key


class _TelemetryResource(resource.ObservableResource):
    """RFC 7641 Observable resource exposing the latest telemetry JSON.

    The resource is notified whenever :meth:`notify` is called with
    new content. We only call :meth:`updated_state` when the payload
    actually changed - otherwise the Observe semantics would degrade
    into a polling loop.
    """

    def __init__(self, room_id: str, initial: Dict[str, Any]):
        super().__init__()
        self._room_id = room_id
        self._last_payload: Dict[str, Any] = dict(initial)

    def notify(self, payload: Dict[str, Any]) -> None:
        if payload == self._last_payload:
            return
        self._last_payload = dict(payload)
        self.updated_state()

    async def render_get(self, request):
        body = json.dumps(self._last_payload).encode("utf-8")
        return Message(
            code=Code.CONTENT,
            payload=body,
            content_format=ContentFormat.JSON,
        )


class _HeartbeatResource(resource.Resource):
    def __init__(self, room_id: str):
        super().__init__()
        self._room_id = room_id
        self._last = int(time.time())

    def touch(self, ts: int) -> None:
        self._last = int(ts)

    async def render_get(self, request):
        body = json.dumps({"sensor_id": self._room_id, "timestamp": self._last, "status": "Healthy"}).encode()
        return Message(code=Code.CONTENT, payload=body, content_format=ContentFormat.JSON)


class _HvacActuatorResource(resource.Resource):
    """Confirmable (CON) PUT endpoint used by the Floor Gateway to toggle
    actuator state on the virtual node. Answers with ACK payload so the
    gateway can republish a success/failure response to HiveMQ.
    """

    def __init__(
        self,
        *,
        room: Room,
        building_id: int,
        on_command: CommandHandler,
        dup_filter: DupFilter,
        log_fn: Callable[[str], None],
    ):
        super().__init__()
        self._room = room
        self._building_id = int(building_id)
        self._on_command = on_command
        self._dup_filter = dup_filter
        self._log = log_fn

    async def render_put(self, request):
        try:
            body = request.payload.decode("utf-8") if request.payload else "{}"
            data = json.loads(body)
        except Exception as e:
            return Message(
                code=Code.BAD_REQUEST,
                payload=json.dumps({"error": f"invalid_json:{e}"}).encode(),
            )

        source = str(getattr(request.remote, "hostinfo", request.remote))
        token = getattr(request, "token", b"") or b""
        key = build_coap_key(source, token)
        if self._dup_filter.seen(key):
            self._log(f"coap.cmd.dedup_drop room_id={self._room.room_id} source={source}")
            return Message(
                code=Code.CHANGED,
                payload=json.dumps({"status": "duplicate_ignored"}).encode(),
                content_format=ContentFormat.JSON,
            )

        room_id = make_room_id(self._building_id, self._room.floor_id, self._room.room_code)
        maybe = self._on_command(room_id, data)
        if asyncio.iscoroutine(maybe):
            await maybe

        ack_body = {"status": "ok", "room_id": room_id, "applied": data}
        return Message(
            code=Code.CHANGED,
            payload=json.dumps(ack_body).encode(),
            content_format=ContentFormat.JSON,
        )


class CoAPTransport(RoomTransport):
    """Aggregate CoAP transport: one aiocoap Context + server per room.

    Each room gets its own UDP port (``base_port + index``) so the Floor
    Gateway can address them individually.  All resources are mounted on
    the single Context but we fan out ports by creating one Context per
    room - this keeps ACL + DTLS PSK isolation straightforward.
    """

    kind = TransportKind.COAP

    def __init__(
        self,
        *,
        rooms: Iterable[Room],
        building_id: int,
        bind_host: str,
        base_port: int,
        topics: TopicScheme,
        dtls_psk: Optional[Dict[str, bytes]] = None,
        dup_filter: Optional[DupFilter] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self._rooms = list(rooms)
        self._building_id = int(building_id)
        self._bind_host = bind_host
        self._base_port = int(base_port)
        self._topics = topics
        self._dtls_psk = dtls_psk or {}
        self._dup_filter = dup_filter or DupFilter()
        self._log = log_fn

        self._handler: Optional[CommandHandler] = None
        self._contexts: Dict[str, aiocoap.Context] = {}
        self._telemetry_resources: Dict[str, _TelemetryResource] = {}
        self._heartbeat_resources: Dict[str, _HeartbeatResource] = {}
        self._ports: Dict[str, int] = {}

    @property
    def connected(self) -> bool:
        return len(self._contexts) == len(self._rooms) and len(self._rooms) > 0

    def register_command_handler(self, handler: CommandHandler) -> None:
        self._handler = handler

    def _ensure_handler(self) -> CommandHandler:
        if self._handler is None:
            raise RuntimeError("CoAPTransport: command handler not registered")
        return self._handler

    def _port_for(self, index: int) -> int:
        return self._base_port + index

    async def start(self) -> None:
        handler = self._ensure_handler()
        for i, room in enumerate(self._rooms):
            room_id = make_room_id(self._building_id, room.floor_id, room.room_code)
            port = self._port_for(i)

            root = resource.Site()

            initial = {
                "sensor_id": room_id,
                "timestamp": int(room.last_update),
                "temperature": float(room.temperature),
                "humidity": float(room.humidity),
                "occupancy": bool(room.occupancy),
                "light_level": int(room.light_level),
                "hvac_mode": str(room.hvac_mode),
                "lighting_dimmer": int(room.lighting_dimmer),
            }

            tel_res = _TelemetryResource(room_id=room_id, initial=initial)
            hb_res = _HeartbeatResource(room_id=room_id)
            act_res = _HvacActuatorResource(
                room=room,
                building_id=self._building_id,
                on_command=handler,
                dup_filter=self._dup_filter,
                log_fn=self._log,
            )

            f = f"f{room.floor_id:02d}"
            r = f"r{room.room_code}"
            root.add_resource((f, r, "telemetry"), tel_res)
            root.add_resource((f, r, "heartbeat"), hb_res)
            root.add_resource((f, r, "actuators", "hvac"), act_res)

            bind = (self._bind_host, port)
            try:
                ctx = await aiocoap.Context.create_server_context(root, bind=bind)
            except Exception as e:
                self._log(f"coap.server.bind_failed room_id={room_id} bind={bind} err={type(e).__name__}:{e}")
                continue

            self._contexts[room_id] = ctx
            self._telemetry_resources[room_id] = tel_res
            self._heartbeat_resources[room_id] = hb_res
            self._ports[room_id] = port
            self._log(f"coap.server.listening room_id={room_id} port={port}")

    async def stop(self) -> None:
        stops: List[asyncio.Task] = []
        for ctx in self._contexts.values():
            stops.append(asyncio.create_task(ctx.shutdown()))
        for t in stops:
            try:
                await t
            except Exception:
                pass
        self._contexts.clear()

    def port_for_room(self, room_id: str) -> Optional[int]:
        return self._ports.get(room_id)

    def publish_telemetry(self, room: Any, payload: Dict[str, Any]) -> None:
        room_id = make_room_id(self._building_id, room.floor_id, room.room_code)
        res = self._telemetry_resources.get(room_id)
        if res is not None:
            res.notify(payload)

    def publish_heartbeat(self, room: Any) -> None:
        room_id = make_room_id(self._building_id, room.floor_id, room.room_code)
        res = self._heartbeat_resources.get(room_id)
        if res is not None:
            res.touch(int(room.last_update))

    async def send_sentinel_alert(
        self,
        room: Any,
        *,
        gateway_host: str,
        gateway_port: int,
        alert_type: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Send a Confirmable (CON) alert from a CoAP room to its Floor
        Gateway's ``/alerts/<room_id>`` endpoint.

        Returns ``True`` on receipt of an ACK, ``False`` on timeout.
        Used for Sentinel events (smoke, intruder, fire) where loss of
        the first packet is unacceptable - aiocoap will retransmit up
        to MAX_RETRANSMIT (default 4) before giving up.
        """

        room_id = make_room_id(self._building_id, room.floor_id, room.room_code)
        ctx = self._contexts.get(room_id)
        if ctx is None:
            self._log(f"coap.alert.no_context room_id={room_id}")
            return False

        body = {
            "sensor_id": room_id,
            "alert": alert_type,
            "timestamp": int(time.time()),
            "details": details or {},
        }
        msg = Message(
            mtype=Type.CON,
            code=Code.POST,
            uri=f"coap://{gateway_host}:{gateway_port}/alerts/{room_id}",
            payload=json.dumps(body).encode("utf-8"),
        )
        msg.opt.content_format = ContentFormat.JSON

        try:
            resp = await ctx.request(msg).response
            self._log(
                f"coap.alert.ack room_id={room_id} code={resp.code} alert={alert_type}"
            )
            return True
        except Exception as e:
            self._log(
                f"coap.alert.timeout room_id={room_id} alert={alert_type} err={type(e).__name__}:{e}"
            )
            return False
