from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import yaml

from .environment import Environment, VirtualClock
from .faults import FaultInjector
from .fleet import FleetManager
from .metrics import MetricsCollector
from .mqtt_client import MQTTClientWrapper
from .physics import PhysicsEngine, ThermalParams
from .persistence import Persistence
from .room import Room


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
    """
    Phase 1 overrides: support a small set of commonly used env vars.
    """
    # Simulation
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

    # MQTT
    if "MQTT_HOST" in os.environ:
        cfg["mqtt"]["broker_host"] = os.environ["MQTT_HOST"]
    if "MQTT_PORT" in os.environ:
        cfg["mqtt"]["broker_port"] = int(os.environ["MQTT_PORT"])
    if "MQTT_TOPIC_PREFIX" in os.environ:
        prefix = os.environ["MQTT_TOPIC_PREFIX"].strip().rstrip("/")
        # Update fleet topics as well.
        cfg["fleet"]["health_topic"] = f"{prefix}/fleet/health"
        cfg["fleet"]["fleet_command_topic"] = f"{prefix}/fleet/command"
        cfg["topics"]["room_telemetry_template"] = f"{prefix}/floor_{{floor}}/room_{{roomCode}}/telemetry"
        cfg["topics"]["room_heartbeat_template"] = f"{prefix}/floor_{{floor}}/room_{{roomCode}}/heartbeat"
        cfg["topics"]["room_command_template"] = f"{prefix}/floor_{{floor}}/room_{{roomCode}}/command"

    # Persistence
    if "SQLITE_PATH" in os.environ:
        cfg["database"]["sqlite_path"] = os.environ["SQLITE_PATH"]

    # Faults
    if "FAULTS_ENABLED" in os.environ:
        cfg["faults"]["enabled"] = os.environ["FAULTS_ENABLED"].lower() in ("1", "true", "yes", "y")
    if "FAULTS_PROBABILITY" in os.environ:
        cfg["faults"]["probability"] = float(os.environ["FAULTS_PROBABILITY"])

    return cfg


def _log_factory(level: str) -> Callable[[str], None]:
    # Phase 1: keep it lightweight; log_fn expects a string (often already JSON).
    def log_fn(message: str) -> None:
        # Include monotonic time for debugging latency without wall-clock changes.
        print(json.dumps({"ts_unix": int(time.time()), "ts_monotonic": time.monotonic(), "level": level, "msg": message}))

    return log_fn


def _stable_seed(room_id: str) -> int:
    # Deterministic seed across processes/runs: use Python's built-in hash is NOT stable.
    import hashlib

    d = hashlib.sha1(room_id.encode("utf-8")).digest()
    return int.from_bytes(d[:4], byteorder="big", signed=False)


async def room_task(
    *,
    room: Room,
    room_id: str,
    command_queue: asyncio.Queue[Dict[str, Any]],
    physics: PhysicsEngine,
    faults_cfg: dict,
    fault_seed: int,
    injector: FaultInjector,
    mqtt: MQTTClientWrapper,
    fleet: FleetManager,
    metrics: MetricsCollector,
    env: Environment,
    tick_interval_real_sec: float,
    tick_dt_sim_sec: float,
    telemetry_interval_ticks: int,
    heartbeat_interval_sec_sim: float,
    max_jitter_sec: float,
    log_fn: Callable[[str], None],
    get_telemetry_topic: Callable[[Room], str],
    get_heartbeat_topic: Callable[[Room], str],
    persistence: Persistence,
    stop_event: asyncio.Event,
) -> None:
    rng = random.Random(fault_seed ^ 0xA5A5A5A5)
    jitter = rng.uniform(0.0, max_jitter_sec) if max_jitter_sec > 0 else 0.0
    if jitter > 0:
        await asyncio.sleep(jitter)

    log_fn(f"room.started room_id={room_id} jitter_ms={jitter*1000:.0f}")

    tick_index = 0
    next_heartbeat_virtual = env.virtual_epoch_now_sec() + float(heartbeat_interval_sec_sim)
    # If telemetry is delayed, we use snapshots.

    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        start = loop.time()
        processing = 0.0
        try:
            # Apply incoming commands at tick start (non-blocking).
            applied_any_command = False
            while True:
                try:
                    payload = command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                room.apply_command(payload)
                applied_any_command = True

            # Update the simulated timestamp for telemetry/heartbeat.
            timestamp = env.virtual_epoch_now_sec()
            room.last_update = int(timestamp)

            # --- Deterministic physics update ---
            physics.update_room(room, tick_dt_sim_sec=tick_dt_sim_sec)

            # --- Fault injection (affects sensor readings, dropout/telemetry delay) ---
            fault_result = injector.tick(
                room=room, tick_index=tick_index, tick_dt_sim_sec=tick_dt_sim_sec
            )

            # Validate/clamp before publishing.
            room.validate_and_clamp()

            # Save point after actuator commands are applied.
            if applied_any_command:
                persistence.request_sync()

            # --- Telemetry publish ---
            publish_telemetry = (telemetry_interval_ticks <= 1) or (tick_index % telemetry_interval_ticks == 0)
            if publish_telemetry and not fault_result.dropout_active:
                payload = room.to_telemetry_json(timestamp=int(timestamp))
                if fault_result.telemetry_delay_sec is not None:
                    delay = float(fault_result.telemetry_delay_sec)
                    topic = get_telemetry_topic(room)
                    payload_snapshot = dict(payload)

                    async def _delayed_pub():
                        await asyncio.sleep(delay)
                        mqtt.publish(topic, json.dumps(payload_snapshot), qos=None)

                    asyncio.create_task(_delayed_pub())
                else:
                    mqtt.publish_telemetry(room, payload)

            # --- Heartbeat publish ---
            if not fault_result.dropout_active and timestamp >= next_heartbeat_virtual:
                mqtt.publish_heartbeat(room)
                fleet.update_heartbeat(room_id)

                # Advance next heartbeat in fixed simulated intervals.
                while timestamp >= next_heartbeat_virtual:
                    next_heartbeat_virtual += float(heartbeat_interval_sec_sim)

            tick_index += 1

        except Exception as e:
            log_fn(f"room_loop.error room_id={room_id} error={type(e).__name__}:{e}")

        processing = loop.time() - start
        metrics.record_tick_processing(processing * 1000.0)
        # Precision drift compensation.
        await asyncio.sleep(max(0.0, tick_interval_real_sec - processing))


async def run_engine(args: argparse.Namespace) -> None:
    # Resolve paths.
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

    # Apply CLI rooms override using FleetManager rooms_limit.
    rooms_limit = args.rooms if args.rooms is not None else None
    rooms_total_expected = int(rooms_limit) if rooms_limit is not None else rooms_total

    tick_interval_real_sec = float(cfg["simulation"]["tick_interval_sec"])
    time_acc = float(cfg["simulation"]["time_acceleration"])
    tick_dt_sim_sec = tick_interval_real_sec * time_acc

    # Virtual clock
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
        ),
        light_occupied_threshold=int(cfg["occupancy_correlation"]["light_occupied_threshold"]),
        light_unoccupied_min=int(cfg["occupancy_correlation"]["light_unoccupied_min"]),
        light_unoccupied_max=int(cfg["occupancy_correlation"]["light_unoccupied_max"]),
        light_occupied_min=int(cfg["occupancy_correlation"]["light_occupied_min"]),
        light_occupied_max=int(cfg["occupancy_correlation"]["light_occupied_max"]),
    )

    # Metrics
    metrics = MetricsCollector(
        latency_threshold_ms=float(cfg["performance"]["latency_threshold_ms"]),
        summary_interval_sec=60.0,
        log_fn=log_fn,
    )

    # Persistence — ensure directory exists (needed for local runs on Windows).
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

    # MQTT topic builders.
    fleet_command_topic = str(cfg["fleet"]["fleet_command_topic"])
    health_topic = str(cfg["fleet"]["health_topic"])
    topic_prefix = "/".join(fleet_command_topic.split("/")[:2])  # campus/bldg_01

    def telemetry_topic(room: Room) -> str:
        return f"{topic_prefix}/floor_{room.floor_id:02d}/room_{room.room_code}/telemetry"

    def heartbeat_topic(room: Room) -> str:
        return f"{topic_prefix}/floor_{room.floor_id:02d}/room_{room.room_code}/heartbeat"

    # For subscription: campus/bldg_01/+/+/command
    room_command_subscription = f"{topic_prefix}/+/+/command"

    # Fleet manager with command queues + heartbeat health.
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
        publish_health_fn=lambda topic, payload: mqtt.publish(topic, json.dumps(payload), qos=None) if mqtt else None,
        health_topic=health_topic,
        heartbeat_timeout_sec=float(cfg["fleet"]["heartbeat_timeout_sec"]),
        heartbeat_check_interval_sec=1.0,
        log_fn=log_fn,
        defaults=defaults,
        rooms_total_expected=rooms_total_expected,
        rooms_limit=rooms_limit,
    )

    # Restore from DB (if available).
    for room_id, room in list(fleet.rooms_by_id.items()):
        row = db_states.get(room_id)
        if not row:
            continue
        fleet.rooms_by_id[room_id] = Room.from_db_row(row)

    # Start MQTT.
    mqtt: Optional[MQTTClientWrapper] = None

    def on_room_command(room_id: str, payload: Dict[str, Any]) -> None:
        fleet.request_room_command(room_id, payload)

    def on_fleet_command(payload: Dict[str, Any]) -> None:
        fleet.request_fleet_command(payload)

    mqtt = MQTTClientWrapper(
        client_id="world-engine",
        broker_host=str(cfg["mqtt"]["broker_host"]),
        broker_port=int(cfg["mqtt"]["broker_port"]) if "broker_port" in cfg["mqtt"] else int(cfg["mqtt"]["broker_port"]),
        qos=int(cfg["mqtt"]["qos"]),
        fleet_command_topic=fleet_command_topic,
        room_command_subscription=room_command_subscription,
        room_telemetry_topic_builder=telemetry_topic,
        room_heartbeat_topic_builder=heartbeat_topic,
        log_fn=log_fn,
        on_room_command=on_room_command,
        on_fleet_command=on_fleet_command,
    )

    await mqtt.connect_and_subscribe()

    # Patch FleetManager health publisher now that mqtt exists.
    fleet._publish_health_fn = lambda topic, payload: mqtt.publish(topic, json.dumps(payload), qos=None)

    # Stop coordination.
    stop_event = asyncio.Event()

    # Metrics background tasks.
    metric_tasks = [
        asyncio.create_task(
            metrics.run_event_loop_latency_monitor(
                stop_event=stop_event, sleep_sec=0.2
            )
        ),
        asyncio.create_task(metrics.run_summary_task(stop_event=stop_event)),
    ]

    # Fleet heartbeat monitor.
    fleet_task = asyncio.create_task(fleet.heartbeat_monitor_task(stop_event=stop_event))

    # Persistence sync loop.
    persistence_task = asyncio.create_task(
        persistence.run_sync_loop(
            rooms_by_id=fleet.rooms_by_id,
            stop_event=stop_event,
            log_fn=log_fn,
        )
    )

    # Room tasks.
    max_jitter_sec = float(cfg["simulation"]["max_startup_jitter_sec"])
    telemetry_interval_ticks = int(cfg["simulation"]["telemetry_interval_ticks"])
    heartbeat_interval_sec_sim = float(cfg["fleet"]["heartbeat_interval_sec"])

    rooms = list(fleet.rooms_by_id.items())
    room_tasks = []
    for room_id, room in rooms:
        q = fleet.command_queues[room_id]
        seed = _stable_seed(room_id)
        injector = FaultInjector(room_seed=seed, config=cfg["faults"], room_id=room_id, log_fn=log_fn)
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
                    mqtt=mqtt,
                    fleet=fleet,
                    metrics=metrics,
                    env=env,
                    tick_interval_real_sec=tick_interval_real_sec,
                    tick_dt_sim_sec=tick_dt_sim_sec,
                    telemetry_interval_ticks=telemetry_interval_ticks,
                    heartbeat_interval_sec_sim=heartbeat_interval_sec_sim,
                    max_jitter_sec=max_jitter_sec,
                    log_fn=log_fn,
                    get_telemetry_topic=telemetry_topic,
                    get_heartbeat_topic=heartbeat_topic,
                    persistence=persistence,
                    stop_event=stop_event,
                )
            )
        )

    # Run for a limited duration if requested.
    try:
        if args.duration_sec is not None:
            await asyncio.sleep(float(args.duration_sec))
            stop_event.set()
        else:
            # Run until Ctrl+C
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

        # Ensure MQTT disconnect.
        try:
            await mqtt.disconnect()
        except Exception:
            pass

        await asyncio.sleep(0.1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 1 World Engine (async IoT campus simulation)")
    p.add_argument("--rooms", type=int, default=None, help="Limit number of rooms for smoke tests")
    p.add_argument("--duration-sec", type=float, default=None, help="Run duration in real seconds then exit")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_engine(args))


if __name__ == "__main__":
    main()

