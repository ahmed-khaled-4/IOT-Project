"""Generate 10 Node-RED flow files (one per floor) for the Phase 2
Floor Gateways.

Run once:

    python gateways/gen_flows.py

Each `gw_fNN.json` contains:
  - MQTT broker config pointing at HiveMQ
  - 10 CoAP Observe clients targeting the 10 CoAP rooms on that floor
  - A CoAP -> MQTT re-publish bridge (telemetry)
  - An MQTT cmd subscriber that proxies to CoAP PUT (CON) per room and
    publishes a .../response message back upstream
  - A 60-second edge-thinning aggregator producing .../summary
  - A DUP flag handler (function node with LRU cache)
  - An offline-autonomy local rule (lights off if unoccupied)

The script is deliberately self-contained and deterministic so the
generated JSON can be committed to git.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List


TAB_ID = "floor-gateway-tab"
MQTT_BROKER_ID = "hivemq-broker-config"

# Per-floor CoAP port layout: base_port + (floor-1)*10 + index
COAP_BASE_PORT = 5684
ENGINE_HOST = "engine"
HIVEMQ_HOST = "hivemq"

ROOMS_PER_FLOOR_COAP = 10
ROOM_CODE_COAP_START = 11  # r{f}11 .. r{f}20


def _node(
    node_id: str,
    node_type: str,
    *,
    name: str = "",
    x: int = 200,
    y: int = 100,
    wires: List[List[str]] | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "z": TAB_ID,
        "name": name,
        "x": x,
        "y": y,
        "wires": wires if wires is not None else [],
    }
    node.update(extra)
    return node


def _tab(label: str) -> Dict[str, Any]:
    return {
        "id": TAB_ID,
        "type": "tab",
        "label": label,
        "disabled": False,
        "info": (
            "Phase 2 Floor Gateway. Bridges CoAP Observe streams (UDP) and "
            "MQTT traffic (TCP) between 20 rooms on this floor and the "
            "HiveMQ backbone, plus 60s edge thinning, offline autonomy, "
            "and MQTT DUP-flag deduplication."
        ),
        "env": [],
    }


def _mqtt_broker_config() -> Dict[str, Any]:
    return {
        "id": MQTT_BROKER_ID,
        "type": "mqtt-broker",
        "name": "hivemq-campus",
        "broker": HIVEMQ_HOST,
        "port": "1883",
        "clientid": "",
        "autoConnect": True,
        "usetls": False,
        "protocolVersion": "5",
        "keepalive": "60",
        "cleansession": True,
        "birthTopic": "",
        "birthQos": "0",
        "birthPayload": "",
        "birthMsg": {},
        "closeTopic": "",
        "closeQos": "0",
        "closePayload": "",
        "closeMsg": {},
        "willTopic": "",
        "willQos": "0",
        "willPayload": "",
        "willMsg": {},
        "sessionExpiry": "",
    }


def _coap_observe_node(
    node_id: str,
    floor: int,
    room_code: int,
    port: int,
    x: int,
    y: int,
    next_id: str,
) -> Dict[str, Any]:
    url = f"coap://{ENGINE_HOST}:{port}/f{floor:02d}/r{room_code}/telemetry"
    return _node(
        node_id,
        "coap request",
        name=f"Observe r{room_code}",
        x=x,
        y=y,
        wires=[[next_id]],
        method="GET",
        observe=True,
        name_payload=False,
        usetls=False,
        **{
            "content-format": "application/json",
            "url": url,
        },
    )


def _func_coap_to_mqtt(node_id: str, floor: int, x: int, y: int, next_id: str) -> Dict[str, Any]:
    code = (
        "// Re-package a CoAP Observe notification as an MQTT telemetry publish\n"
        "// for the same room. Keeps topic structure identical to native MQTT\n"
        "// nodes so ThingsBoard sees a unified stream.\n"
        "let payload = msg.payload;\n"
        "if (Buffer.isBuffer(payload)) {\n"
        "    try { payload = JSON.parse(payload.toString('utf8')); } catch (e) { return null; }\n"
        "}\n"
        "if (typeof payload === 'string') {\n"
        "    try { payload = JSON.parse(payload); } catch (e) { return null; }\n"
        "}\n"
        "const sensorId = (payload && payload.sensor_id) || '';\n"
        "const m = sensorId.match(/^b(\\d{2})-f(\\d{2})-r(\\d+)$/);\n"
        "if (!m) { return null; }\n"
        f"const floor = '{floor:02d}';\n"
        "const roomCode = m[3];\n"
        "msg.topic = `campus/b01/f${floor}/r${roomCode}/telemetry`;\n"
        "msg.qos = 1;\n"
        "msg.retain = false;\n"
        "msg.payload = payload;\n"
        "// Track for edge thinning\n"
        "let bucket = flow.get('thinBucket') || {temp: [], hum: [], n: 0};\n"
        "if (typeof payload.temperature === 'number') bucket.temp.push(payload.temperature);\n"
        "if (typeof payload.humidity === 'number') bucket.hum.push(payload.humidity);\n"
        "bucket.n += 1;\n"
        "flow.set('thinBucket', bucket);\n"
        "return msg;\n"
    )
    return _node(
        node_id,
        "function",
        name=f"coap->mqtt f{floor:02d}",
        x=x,
        y=y,
        wires=[[next_id]],
        func=code,
        outputs=1,
        noerr=0,
        initialize="",
        finalize="",
        libs=[],
    )


def _mqtt_out(node_id: str, name: str, x: int, y: int, topic: str = "") -> Dict[str, Any]:
    return _node(
        node_id,
        "mqtt out",
        name=name,
        x=x,
        y=y,
        wires=[],
        topic=topic,
        qos="",
        retain="",
        respTopic="",
        contentType="",
        userProps="",
        correl="",
        expiry="",
        broker=MQTT_BROKER_ID,
    )


def _mqtt_in(node_id: str, topic: str, name: str, x: int, y: int, next_id: str, qos: str = "2") -> Dict[str, Any]:
    return _node(
        node_id,
        "mqtt in",
        name=name,
        x=x,
        y=y,
        wires=[[next_id]],
        topic=topic,
        qos=qos,
        datatype="auto-detect",
        broker=MQTT_BROKER_ID,
        nl=False,
        rap=True,
        rh=0,
        inputs=0,
    )


def _func_dup_filter(node_id: str, x: int, y: int, next_id: str) -> Dict[str, Any]:
    code = (
        "// Drop MQTT commands whose DUP flag is set AND that we've already\n"
        "// processed within the last 60 seconds. Uses a per-flow LRU map\n"
        "// keyed by (topic, command_id).\n"
        "const CACHE_KEY = 'dupCache';\n"
        "const cache = flow.get(CACHE_KEY) || {};\n"
        "const now = Date.now();\n"
        "const ttl = 60 * 1000;\n"
        "// Evict expired\n"
        "for (const k of Object.keys(cache)) {\n"
        "    if (cache[k] < now - ttl) delete cache[k];\n"
        "}\n"
        "const payload = (typeof msg.payload === 'string') ? (function () { try { return JSON.parse(msg.payload); } catch (e) { return null; } })() : msg.payload;\n"
        "if (!payload || typeof payload !== 'object') { return null; }\n"
        "const cmdId = payload.command_id || `${msg.topic}:${msg._msgid}`;\n"
        "const dupFlag = !!(msg.dup || (msg.fixed && msg.fixed.dup));\n"
        "const key = `${msg.topic}|${cmdId}`;\n"
        "if (cache[key] && dupFlag) {\n"
        "    node.warn(`DUP drop key=${key}`);\n"
        "    return null;\n"
        "}\n"
        "cache[key] = now;\n"
        "flow.set(CACHE_KEY, cache);\n"
        "msg.payload = payload;\n"
        "return msg;\n"
    )
    return _node(
        node_id,
        "function",
        name="DUP filter",
        x=x,
        y=y,
        wires=[[next_id]],
        func=code,
        outputs=1,
        noerr=0,
        libs=[],
    )


def _func_cmd_to_coap(node_id: str, floor: int, x: int, y: int, next_id: str) -> Dict[str, Any]:
    port_map_entries = []
    for idx, room_code in enumerate(range(floor * 100 + ROOM_CODE_COAP_START, floor * 100 + ROOM_CODE_COAP_START + ROOMS_PER_FLOOR_COAP)):
        port = COAP_BASE_PORT + (floor - 1) * 10 + idx
        port_map_entries.append(f"    {room_code}: {port},")
    port_map = "const PORT_MAP = {\n" + "\n".join(port_map_entries) + "\n};\n"

    code = (
        "// Translate MQTT command (campus/b01/fNN/rXXX/cmd) into a CoAP PUT\n"
        "// (Confirmable) request aimed at the virtual node's /actuators/hvac\n"
        "// resource. Emits the CoAP request message for the coap-request node.\n"
        f"{port_map}"
        "const m = (msg.topic || '').match(/^campus\\/b01\\/f(\\d{2})\\/r(\\d+)\\/cmd$/);\n"
        "if (!m) { return null; }\n"
        f"const floorStr = '{floor:02d}';\n"
        "const roomCode = parseInt(m[2], 10);\n"
        "const port = PORT_MAP[roomCode];\n"
        "if (!port) {\n"
        "    node.warn(`cmd for unknown CoAP room ${roomCode}`);\n"
        "    return null;\n"
        "}\n"
        "const engineHost = global.get('engineHost') || 'engine';\n"
        "msg.method = 'PUT';\n"
        "msg.observe = false;\n"
        "msg.options = { 'Content-Format': 'application/json' };\n"
        "msg.url = `coap://${engineHost}:${port}/f${floorStr}/r${roomCode}/actuators/hvac`;\n"
        "msg._cmd_topic = msg.topic;\n"
        "msg._room_code = roomCode;\n"
        "msg._floor = floorStr;\n"
        "// Keep payload as JSON object; coap-request serialises\n"
        "return msg;\n"
    )
    return _node(
        node_id,
        "function",
        name=f"cmd->CoAP PUT f{floor:02d}",
        x=x,
        y=y,
        wires=[[next_id]],
        func=code,
        outputs=1,
        noerr=0,
        libs=[],
    )


def _coap_put_node(node_id: str, x: int, y: int, next_id: str) -> Dict[str, Any]:
    return _node(
        node_id,
        "coap request",
        name="CoAP PUT CON",
        x=x,
        y=y,
        wires=[[next_id]],
        method="PUT",
        observe=False,
        name_payload=True,
        usetls=False,
        **{"content-format": "application/json", "url": ""},
    )


def _func_coap_ack_to_response(node_id: str, x: int, y: int, next_id: str) -> Dict[str, Any]:
    code = (
        "// Received a CoAP ACK from the room; republish a .../response\n"
        "// message so ThingsBoard can confirm the command landed.\n"
        "let payload = msg.payload;\n"
        "if (Buffer.isBuffer(payload)) {\n"
        "    try { payload = JSON.parse(payload.toString('utf8')); } catch (e) { payload = { raw: payload.toString('utf8') }; }\n"
        "}\n"
        "const topicBase = (msg._cmd_topic || '').replace(/\\/cmd$/, '');\n"
        "if (!topicBase) return null;\n"
        "msg.topic = `${topicBase}/response`;\n"
        "msg.qos = 1;\n"
        "msg.retain = false;\n"
        "msg.payload = { status: 'ok', from: 'coap-ack', data: payload };\n"
        "return msg;\n"
    )
    return _node(
        node_id,
        "function",
        name="CoAP ACK -> MQTT response",
        x=x,
        y=y,
        wires=[[next_id]],
        func=code,
        outputs=1,
        noerr=0,
        libs=[],
    )


def _inject_tick(node_id: str, name: str, interval_sec: int, x: int, y: int, next_id: str) -> Dict[str, Any]:
    return _node(
        node_id,
        "inject",
        name=name,
        x=x,
        y=y,
        wires=[[next_id]],
        props=[{"p": "payload"}, {"p": "topic", "vt": "str"}],
        repeat=str(interval_sec),
        crontab="",
        once=False,
        onceDelay=str(interval_sec),
        topic="tick",
        payload="",
        payloadType="date",
    )


def _func_edge_thinning(node_id: str, floor: int, x: int, y: int, next_id: str) -> Dict[str, Any]:
    code = (
        "// Compute and publish a 60-second floor-level state summary to\n"
        "// reduce network congestion (campus/b01/fNN/summary).  Also\n"
        "// powers ThingsBoard's Floor Health card.\n"
        "const bucket = flow.get('thinBucket') || {temp: [], hum: [], n: 0};\n"
        "function avg(xs) { return xs.length ? xs.reduce((a,b)=>a+b,0) / xs.length : null; }\n"
        "const summary = {\n"
        f"    floor: 'f{floor:02d}',\n"
        "    window_sec: 60,\n"
        "    samples: bucket.n,\n"
        "    avg_temperature: avg(bucket.temp),\n"
        "    avg_humidity: avg(bucket.hum),\n"
        "    timestamp: Math.floor(Date.now() / 1000)\n"
        "};\n"
        "flow.set('thinBucket', {temp: [], hum: [], n: 0});\n"
        f"msg.topic = 'campus/b01/f{floor:02d}/summary';\n"
        "msg.qos = 1;\n"
        "msg.retain = true;\n"
        "msg.payload = summary;\n"
        "return msg;\n"
    )
    return _node(
        node_id,
        "function",
        name="Edge thinning (60s)",
        x=x,
        y=y,
        wires=[[next_id]],
        func=code,
        outputs=1,
        noerr=0,
        libs=[],
    )


def _func_offline_autonomy(node_id: str, floor: int, x: int, y: int, next_id: str) -> Dict[str, Any]:
    code = (
        "// Local autonomy: if a room reports occupancy=false AND the cloud\n"
        "// connection is degraded, emit a lights-off MQTT cmd locally so\n"
        "// the gateway keeps controlling actuators even when offline.\n"
        "const payload = msg.payload || {};\n"
        "if (typeof payload !== 'object') return null;\n"
        "const cloudOk = (flow.get('cloudOk') !== false);\n"
        "if (payload.occupancy === false && payload.lighting_dimmer > 10 && !cloudOk) {\n"
        "    const sid = payload.sensor_id || '';\n"
        "    const m = sid.match(/^b(\\d{2})-f(\\d{2})-r(\\d+)$/);\n"
        "    if (!m) return null;\n"
        "    const local = { topic: `campus/b01/f${m[2]}/r${m[3]}/cmd`, qos: 2, payload: {lighting_dimmer: 0, command_id: `autonomy-${sid}-${Date.now()}`} };\n"
        "    return local;\n"
        "}\n"
        "return null;\n"
    )
    return _node(
        node_id,
        "function",
        name="Offline autonomy",
        x=x,
        y=y,
        wires=[[next_id]],
        func=code,
        outputs=1,
        noerr=0,
        libs=[],
    )


def _status_watchdog(node_id: str, x: int, y: int) -> Dict[str, Any]:
    return _node(
        node_id,
        "status",
        name="Cloud status watchdog",
        x=x,
        y=y,
        wires=[[]],
        scope=[MQTT_BROKER_ID],
    )


def generate_flow(floor: int) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []

    nodes.append(_tab(f"Floor {floor:02d} Gateway"))
    nodes.append(_mqtt_broker_config())

    # --- CoAP Observe ingest ---
    func_coap_to_mqtt_id = f"f{floor:02d}-func-coap2mqtt"
    mqtt_tel_out_id = f"f{floor:02d}-mqtt-tel-out"

    nodes.append(_mqtt_out(mqtt_tel_out_id, f"tel-out-f{floor:02d}", x=900, y=200))
    nodes.append(_func_coap_to_mqtt(func_coap_to_mqtt_id, floor, x=600, y=200, next_id=mqtt_tel_out_id))

    for idx in range(ROOMS_PER_FLOOR_COAP):
        room_code = floor * 100 + ROOM_CODE_COAP_START + idx
        port = COAP_BASE_PORT + (floor - 1) * 10 + idx
        nodes.append(
            _coap_observe_node(
                f"f{floor:02d}-coap-obs-r{room_code}",
                floor=floor,
                room_code=room_code,
                port=port,
                x=250,
                y=100 + idx * 40,
                next_id=func_coap_to_mqtt_id,
            )
        )

    # --- MQTT command -> CoAP PUT pipeline ---
    dup_id = f"f{floor:02d}-func-dup"
    cmd2coap_id = f"f{floor:02d}-func-cmd2coap"
    coap_put_id = f"f{floor:02d}-coap-put"
    ack2resp_id = f"f{floor:02d}-func-ack2resp"
    mqtt_resp_out_id = f"f{floor:02d}-mqtt-resp-out"
    mqtt_cmd_in_id = f"f{floor:02d}-mqtt-cmd-in"

    nodes.append(_mqtt_out(mqtt_resp_out_id, f"resp-out-f{floor:02d}", x=1100, y=520))
    nodes.append(_func_coap_ack_to_response(ack2resp_id, x=880, y=520, next_id=mqtt_resp_out_id))
    nodes.append(_coap_put_node(coap_put_id, x=700, y=520, next_id=ack2resp_id))
    nodes.append(_func_cmd_to_coap(cmd2coap_id, floor=floor, x=500, y=520, next_id=coap_put_id))
    nodes.append(_func_dup_filter(dup_id, x=340, y=520, next_id=cmd2coap_id))
    nodes.append(_mqtt_in(mqtt_cmd_in_id, topic=f"campus/b01/f{floor:02d}/+/cmd", name=f"cmd-in-f{floor:02d}", x=150, y=520, next_id=dup_id, qos="2"))

    # --- Edge thinning: inject every 60s ---
    thinning_id = f"f{floor:02d}-func-thinning"
    mqtt_summary_out_id = f"f{floor:02d}-mqtt-summary-out"
    tick_id = f"f{floor:02d}-inject-tick"

    nodes.append(_mqtt_out(mqtt_summary_out_id, f"summary-out-f{floor:02d}", x=900, y=700))
    nodes.append(_func_edge_thinning(thinning_id, floor=floor, x=600, y=700, next_id=mqtt_summary_out_id))
    nodes.append(_inject_tick(tick_id, name="every-60s", interval_sec=60, x=250, y=700, next_id=thinning_id))

    # --- Offline autonomy + status watchdog ---
    autonomy_id = f"f{floor:02d}-func-autonomy"
    autonomy_local_out_id = f"f{floor:02d}-mqtt-autonomy-out"
    status_id = f"f{floor:02d}-status"

    nodes.append(_status_watchdog(status_id, x=250, y=820))
    nodes.append(_mqtt_out(autonomy_local_out_id, f"autonomy-out-f{floor:02d}", x=900, y=820))
    nodes.append(_func_offline_autonomy(autonomy_id, floor=floor, x=600, y=820, next_id=autonomy_local_out_id))

    # Re-feed the coap->mqtt output into autonomy check (via a link)
    # For simplicity wire MQTT tel-out is terminal; autonomy picks from flow context via bucket.
    # Attach autonomy to the same mqtt-tel input stream by extending wires from coap->mqtt node:
    for node in nodes:
        if node.get("id") == func_coap_to_mqtt_id:
            node["wires"] = [[mqtt_tel_out_id, autonomy_id]]
            break

    return nodes


def main() -> None:
    out_dir = os.path.join(os.path.dirname(__file__), "flows")
    os.makedirs(out_dir, exist_ok=True)

    for floor in range(1, 11):
        flow = generate_flow(floor)
        path = os.path.join(out_dir, f"gw_f{floor:02d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(flow, f, indent=2)
        print(f"wrote {path} ({len(flow)} nodes)")


if __name__ == "__main__":
    main()
