from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from .environment import Environment, VirtualClock
from .faults import FaultInjector
from .fleet import FleetManager
from .metrics import MetricsCollector
from .ota import OTAManager
from .physics import PhysicsEngine, ThermalParams
from .persistence import Persistence
from .room import Room
from .security import build_mqtt_tls_ctx, load_coap_psk, load_mqtt_credentials
from .shadow import ShadowManager
from .topics import TopicScheme
from .transport import DupFilter, RoomTransport, TransportKind
from .transport.coap_transport import CoAPTransport
from .transport.mqtt_transport import MQTTTransport


# ---------------------------------------------------------------------------
# dotenv + env overrides
# ---------------------------------------------------------------------------


def _load_dotenv(path: str, *, overwrite: bool = False) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if overwrite or k not in os.environ:
                os.environ[k] = v


def _deep_get(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _apply_env_overrides(cfg: dict) -> dict:
    if "SIM_TICK_INTERVAL" in os.environ:
        cfg["simulation"]["tick_interval_sec"] = float(os.environ["SIM_TICK_INTERVAL"])
    if "SIM_TIME_ACCELERATION" in os.environ:
        cfg["simulation"]["time_acceleration"] = float(os.environ["SIM_TIME_ACCELERATION"])
    if "SIM_MAX_STARTUP_JITTER_SEC" in os.environ:
        cfg["simulation"]["max_startup_jitter_sec"] = float(os.environ["SIM_MAX_STARTUP_JITTER_SEC"])
    if "SIM_PERSISTENCE_SYNC_INTERVAL" in os.environ:
        cfg["simulation"]["persistence_sync_interval_sec"] = float(
            os.environ["SIM_PERSISTENCE_SYNC_INTERVAL"]
        )

    if "MQTT_HOST" in os.environ:
        cfg["mqtt"]["broker_host"] = os.environ["MQTT_HOST"]
    if "MQTT_PORT" in os.environ:
        cfg["mqtt"]["broker_port"] = int(os.environ["MQTT_PORT"])
    if "MQTT_TLS" in os.environ:
        cfg["mqtt"]["tls_enabled"] = os.environ["MQTT_TLS"].lower() in ("1", "true", "yes")
    if "MQTT_TOPIC_PREFIX" in os.environ:
        cfg["topics"]["prefix"] = os.environ["MQTT_TOPIC_PREFIX"].strip().rstrip("/")

    if "COAP_BASE_PORT" in os.environ:
        cfg["coap"]["base_port"] = int(os.environ["COAP_BASE_PORT"])
    if "COAP_BIND" in os.environ:
        cfg["coap"]["bind_host"] = os.environ["COAP_BIND"]

    if "SQLITE_PATH" in os.environ:
        cfg["database"]["sqlite_path"] = os.environ["SQLITE_PATH"]

    if "FAULTS_ENABLED" in os.environ:
        cfg["faults"]["enabled"] = os.environ["FAULTS_ENABLED"].lower() in ("1", "true", "yes", "y")
    if "FAULTS_PROBABILITY" in os.environ:
        cfg["faults"]["probability"] = float(os.environ["FAULTS_PROBABILITY"])

    return cfg


def _log_factory(level: str) -> Callable[[str], None]:
    def log_fn(message: str) -> None:
        print(
            json.dumps(
                {
                    "ts_unix": int(time.time()),
                    "ts_monotonic": time.monotonic(),
                    "level": level,
                    "msg": message,
                }
            )
        )

    return log_fn


def _stable_seed(room_id: str) -> int:
    import hashlib

    d = hashlib.sha1(room_id.encode("utf-8")).digest()
    return int.from_bytes(d[:4], byteorder="big", signed=False)


# ---------------------------------------------------------------------------
# Transport routing: decide MQTT vs CoAP for each room
# ---------------------------------------------------------------------------


def _classify_room(room: Room, cfg: dict) -> TransportKind:
    """Rooms r{floor}01..r{floor}10 are MQTT, r{floor}11..r{floor}20 are CoAP."""

    mqtt_range = cfg["transport"]["mqtt_index_range"]
    coap_range = cfg["transport"]["coap_index_range"]

    index_on_floor = room.room_code - room.floor_id * 100
    if mqtt_range[0] <= index_on_floor <= mqtt_range[1]:
        return TransportKind.MQTT
    if coap_range[0] <= index_on_floor <= coap_range[1]:
        return TransportKind.COAP

    raise ValueError(
        f"Room {room.room_id} index_on_floor={index_on_floor} doesn't fit either transport range"
    )


# ---------------------------------------------------------------------------
# Room task: transport-agnostic
# ---------------------------------------------------------------------------


async def room_task(
    *,
    room: Room,
    room_id: str,
    command_queue: asyncio.Queue,
    physics: PhysicsEngine,
    faults_cfg: dict,
    fault_seed: int,
    injector: FaultInjector,
    transport: RoomTransport,
    fleet: FleetManager,
    metrics: MetricsCollector,
    env: Environment,
    tick_interval_real_sec: float,
    tick_dt_sim_sec: float,
    telemetry_interval_ticks: int,
    heartbeat_interval_sec_sim: float,
    max_jitter_sec: float,
    log_fn: Callable[[str], None],
    persistence: Persistence,
    stop_event: asyncio.Event,
    shadow_manager: Optional[ShadowManager] = None,
) -> None:
    rng = random.Random(fault_seed ^ 0xA5A5A5A5)
    jitter = rng.uniform(0.0, max_jitter_sec) if max_jitter_sec > 0 else 0.0
    if jitter > 0:
        await asyncio.sleep(jitter)

    log_fn(
        f"room.started room_id={room_id} transport={transport.kind.value} jitter_ms={jitter * 1000:.0f}"
    )

    tick_index = 0
    next_heartbeat_virtual = env.virtual_epoch_now_sec() + float(heartbeat_interval_sec_sim)

    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        start = loop.time()
        processing = 0.0
        try:
            applied_any_command = False
            while True:
                try:
                    payload = command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                room.apply_command(payload)
                applied_any_command = True

            timestamp = env.virtual_epoch_now_sec()
            room.last_update = int(timestamp)

            physics.update_room(room, tick_dt_sim_sec=tick_dt_sim_sec)

            fault_result = injector.tick(
                room=room, tick_index=tick_index, tick_dt_sim_sec=tick_dt_sim_sec
            )

            room.validate_and_clamp()

            if applied_any_command:
                persistence.request_sync()

            publish_telemetry = (telemetry_interval_ticks <= 1) or (
                tick_index % telemetry_interval_ticks == 0
            )
            if publish_telemetry and not fault_result.dropout_active:
                payload = room.to_telemetry_json(timestamp=int(timestamp))
                if fault_result.telemetry_delay_sec is not None:
                    delay = float(fault_result.telemetry_delay_sec)
                    payload_snapshot = dict(payload)

                    async def _delayed_pub():
                        await asyncio.sleep(delay)
                        transport.publish_telemetry(room, payload_snapshot)

                    asyncio.create_task(_delayed_pub())
                else:
                    transport.publish_telemetry(room, payload)

            if not fault_result.dropout_active and timestamp >= next_heartbeat_virtual:
                transport.publish_heartbeat(room)
                fleet.update_heartbeat(room_id)

                # Phase 3: publish client attributes (reported state + version) for TB shadow sync
                if hasattr(transport, "publish_client_attributes"):
                    transport.publish_client_attributes(room)

                # Phase 3: reconcile shadow state — push desired to command queue if out of sync
                if shadow_manager is not None:
                    shadow_manager.apply_desired(room)

                while timestamp >= next_heartbeat_virtual:
                    next_heartbeat_virtual += float(heartbeat_interval_sec_sim)

            tick_index += 1

        except Exception as e:
            log_fn(f"room_loop.error room_id={room_id} error={type(e).__name__}:{e}")

        processing = loop.time() - start
        metrics.record_tick_processing(processing * 1000.0)
        await asyncio.sleep(max(0.0, tick_interval_real_sec - processing))


# ---------------------------------------------------------------------------
# Engine bootstrap
# ---------------------------------------------------------------------------


async def run_engine(args: argparse.Namespace) -> None:
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    config_path = os.path.join(root_dir, "config", "config.yaml")
    dotenv_path = os.path.join(root_dir, ".env")

    _load_dotenv(dotenv_path, overwrite=False)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg = _apply_env_overrides(cfg)

    level = str(_deep_get(cfg, "logging.level", "INFO"))
    log_fn = _log_factory(level)

    buildings = int(cfg["campus"]["buildings"])
    floors_per_building = int(cfg["campus"]["floors_per_building"])
    rooms_per_floor = int(cfg["campus"]["rooms_per_floor"])
    rooms_total = buildings * floors_per_building * rooms_per_floor

    rooms_limit = args.rooms if args.rooms is not None else None
    rooms_total_expected = int(rooms_limit) if rooms_limit is not None else rooms_total

    tick_interval_real_sec = float(cfg["simulation"]["tick_interval_sec"])
    time_acc = float(cfg["simulation"]["time_acceleration"])
    tick_dt_sim_sec = tick_interval_real_sec * time_acc

    clock = VirtualClock(
        start_real_epoch_sec=time.time(),
        start_monotonic=time.monotonic(),
        time_acceleration=time_acc,
    )
    env = Environment(
        clock,
        outside_base=float(cfg["thermal"]["outside_temp_base"]),
        outside_amplitude=float(cfg["thermal"]["outside_temp_amplitude"]),
    )

    physics = PhysicsEngine(
        env,
        ThermalParams(
            alpha=float(cfg["thermal"]["alpha"]),
            beta=float(cfg["thermal"]["beta"]),
            occupancy_heat=float(cfg["thermal"]["occupancy_heat"]),
            humidity_gain=float(cfg["thermal"]["humidity_gain"]),
            default_humidity=float(cfg["thermal"]["default_humidity"]),
            humidity_pull=float(cfg["thermal"].get("humidity_pull", 0.03)),
        ),
        light_occupied_threshold=int(cfg["occupancy_correlation"]["light_occupied_threshold"]),
        light_unoccupied_min=int(cfg["occupancy_correlation"]["light_unoccupied_min"]),
        light_unoccupied_max=int(cfg["occupancy_correlation"]["light_unoccupied_max"]),
        light_occupied_min=int(cfg["occupancy_correlation"]["light_occupied_min"]),
        light_occupied_max=int(cfg["occupancy_correlation"]["light_occupied_max"]),
    )

    metrics = MetricsCollector(
        latency_threshold_ms=float(cfg["performance"]["latency_threshold_ms"]),
        summary_interval_sec=60.0,
        log_fn=log_fn,
    )

    db_path = str(cfg["database"]["sqlite_path"])
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    persistence = Persistence(
        db_path=db_path,
        rooms_total_expected=rooms_total_expected,
        sync_interval_sec=float(cfg["simulation"]["persistence_sync_interval_sec"]),
    )
    await persistence.init_db()
    db_states = await persistence.load_room_states()
    log_fn(f"persistence.loaded_rooms={len(db_states)}")

    topics = TopicScheme(prefix=str(cfg["topics"]["prefix"]))

    defaults = {
        "default_temp": float(cfg["thermal"]["default_temp"]),
        "default_humidity": float(cfg["thermal"]["default_humidity"]),
        "default_hvac_mode": str(cfg["hvac"]["default_hvac_mode"]),
        "default_target_temp": float(cfg["hvac"]["default_target_temp"]),
        "lighting_dimmer_default": int(cfg["hvac"]["lighting_dimmer_default"]),
    }

    fleet = FleetManager(
        buildings=buildings,
        floors_per_building=floors_per_building,
        rooms_per_floor=rooms_per_floor,
        clock=clock,
        persistence=persistence,
        publish_health_fn=lambda topic, payload: None,  # wired after MQTT transport is up
        health_topic=topics.fleet_health(),
        heartbeat_timeout_sec=float(cfg["fleet"]["heartbeat_timeout_sec"]),
        heartbeat_check_interval_sec=1.0,
        log_fn=log_fn,
        defaults=defaults,
        rooms_total_expected=rooms_total_expected,
        rooms_limit=rooms_limit,
    )

    for room_id, room in list(fleet.rooms_by_id.items()):
        row = db_states.get(room_id)
        if not row:
            continue
        fleet.rooms_by_id[room_id] = Room.from_db_row(row)

    mqtt_rooms: List[Room] = []
    coap_rooms: List[Room] = []
    for _rid, room in fleet.rooms_by_id.items():
        try:
            kind = _classify_room(room, cfg)
        except ValueError as e:
            log_fn(f"transport.classify_error {e}")
            continue
        (mqtt_rooms if kind is TransportKind.MQTT else coap_rooms).append(room)

    log_fn(
        f"transport.routing mqtt_rooms={len(mqtt_rooms)} coap_rooms={len(coap_rooms)}"
    )

    # Shared DUP filter used by both transports
    dup_filter = DupFilter(max_size=4096, ttl_sec=120.0)

    def on_room_command(room_id: str, payload: Dict[str, Any]) -> None:
        if room_id == "__fleet__":
            fleet.request_fleet_command(payload)
            return
        fleet.request_room_command(room_id, payload)

    tls_enabled = bool(cfg["mqtt"].get("tls_enabled", False))
    dtls_enabled = bool(cfg["coap"].get("dtls_enabled", False))

    mqtt_tls_ctx = build_mqtt_tls_ctx() if tls_enabled else None
    mqtt_credentials = load_mqtt_credentials()
    coap_psks = load_coap_psk() if dtls_enabled else {}

    if tls_enabled and mqtt_tls_ctx is None:
        log_fn("security.tls_requested_but_ca_missing falling back to plain MQTT")
    if mqtt_credentials:
        log_fn(f"security.mqtt_credentials_loaded count={len(mqtt_credentials)}")
    if coap_psks:
        log_fn(f"security.coap_psks_loaded count={len(coap_psks)}")

    # Phase 3: Shadow state manager (desired vs reported reconciliation)
    shadow_manager = ShadowManager(
        command_queues=fleet.command_queues,
        log_fn=log_fn,
        desired_hvac_key=str(cfg.get("shadow", {}).get("desired_hvac_key", "desired_hvac_mode")),
        desired_dimmer_key=str(cfg.get("shadow", {}).get("desired_dimmer_key", "desired_lighting_dimmer")),
    )

    mqtt_transport = MQTTTransport(
        rooms=mqtt_rooms,
        building_id=1,
        broker_host=str(cfg["mqtt"]["broker_host"]),
        broker_port=int(cfg["mqtt"].get("broker_tls_port", 8883) if tls_enabled and mqtt_tls_ctx else cfg["mqtt"]["broker_port"]),
        topics=topics,
        qos_telemetry=int(cfg["mqtt"].get("qos_telemetry", 1)),
        qos_command=int(cfg["mqtt"].get("qos_command", 2)),
        tls_ctx=mqtt_tls_ctx,
        credentials=mqtt_credentials or None,
        dup_filter=dup_filter,
        log_fn=log_fn,
        shadow_manager=shadow_manager,
    )
    mqtt_transport.register_command_handler(on_room_command)

    # Phase 3: OTA manager — wired after mqtt_transport so it can publish alerts via the fleet client
    def _publish_ota_alert(payload: Dict[str, Any]) -> None:
        mqtt_transport.publish_ota_alert(payload)

    ota_manager = OTAManager(
        physics=physics,
        rooms_by_id=fleet.rooms_by_id,
        log_fn=log_fn,
        publish_alert_fn=_publish_ota_alert,
        prefix=str(cfg["topics"]["prefix"]),
    )
    # Inject ota_manager into the already-constructed transport (before start())
    mqtt_transport._ota = ota_manager

    coap_transport = CoAPTransport(
        rooms=coap_rooms,
        building_id=1,
        bind_host=str(cfg["coap"].get("bind_host", "0.0.0.0")),
        base_port=int(cfg["coap"].get("base_port", 5684)),
        topics=topics,
        dtls_psk=coap_psks,
        dup_filter=dup_filter,
        log_fn=log_fn,
    )
    coap_transport.register_command_handler(on_room_command)

    await mqtt_transport.start()
    await coap_transport.start()

    # Now wire fleet health publishing through the MQTT transport.
    fleet._publish_health_fn = lambda topic, payload: mqtt_transport.publish_fleet_health(payload)

    # Map room_id -> its transport for the per-room coroutines.
    transport_for: Dict[str, RoomTransport] = {}
    for r in mqtt_rooms:
        transport_for[r.room_id] = mqtt_transport
    for r in coap_rooms:
        transport_for[r.room_id] = coap_transport

    stop_event = asyncio.Event()

    metric_tasks = [
        asyncio.create_task(
            metrics.run_event_loop_latency_monitor(stop_event=stop_event, sleep_sec=0.2)
        ),
        asyncio.create_task(metrics.run_summary_task(stop_event=stop_event)),
    ]

    fleet_task = asyncio.create_task(fleet.heartbeat_monitor_task(stop_event=stop_event))

    persistence_task = asyncio.create_task(
        persistence.run_sync_loop(
            rooms_by_id=fleet.rooms_by_id,
            stop_event=stop_event,
            log_fn=log_fn,
        )
    )

    max_jitter_sec = float(cfg["simulation"]["max_startup_jitter_sec"])
    telemetry_interval_ticks = int(cfg["simulation"]["telemetry_interval_ticks"])
    heartbeat_interval_sec_sim = float(cfg["fleet"]["heartbeat_interval_sec"])

    room_tasks = []
    for room_id, room in fleet.rooms_by_id.items():
        q = fleet.command_queues[room_id]
        seed = _stable_seed(room_id)
        injector = FaultInjector(
            room_seed=seed, config=cfg["faults"], room_id=room_id, log_fn=log_fn
        )
        tr = transport_for.get(room_id)
        if tr is None:
            log_fn(f"transport.missing_for_room room_id={room_id}")
            continue
        room_tasks.append(
            asyncio.create_task(
                room_task(
                    room=room,
                    room_id=room_id,
                    command_queue=q,
                    physics=physics,
                    faults_cfg=cfg["faults"],
                    fault_seed=seed,
                    injector=injector,
                    transport=tr,
                    fleet=fleet,
                    metrics=metrics,
                    env=env,
                    tick_interval_real_sec=tick_interval_real_sec,
                    tick_dt_sim_sec=tick_dt_sim_sec,
                    telemetry_interval_ticks=telemetry_interval_ticks,
                    heartbeat_interval_sec_sim=heartbeat_interval_sec_sim,
                    max_jitter_sec=max_jitter_sec,
                    log_fn=log_fn,
                    persistence=persistence,
                    stop_event=stop_event,
                    shadow_manager=shadow_manager,
                )
            )
        )

    try:
        if args.duration_sec is not None:
            await asyncio.sleep(float(args.duration_sec))
            stop_event.set()
        else:
            while True:
                await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()

        for t in room_tasks:
            t.cancel()
        for t in metric_tasks:
            t.cancel()
        fleet_task.cancel()
        persistence_task.cancel()

        try:
            await mqtt_transport.stop()
        except Exception:
            pass
        try:
            await coap_transport.stop()
        except Exception:
            pass

        await asyncio.sleep(0.1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 2 Hybrid World Engine (MQTT + CoAP)")
    p.add_argument("--rooms", type=int, default=None, help="Limit room count for testing")
    p.add_argument("--duration-sec", type=float, default=None, help="Run duration in seconds")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_engine(args))


if __name__ == "__main__":
    main()
