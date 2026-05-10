"""Phase 3: OTA update push tool.

Publishes a signed OTA configuration payload to the campus MQTT broker.
The World Engine subscribes to OTA topics and verifies the SHA-256 signature
before applying the new physics parameters.

Usage examples:

    # Broadcast to all 200 rooms (on host-exposed HiveMQ port 1885)
    python tools/ota_push.py --version 1.1 --alpha 0.012 --beta 0.18 --target all

    # Floor 05 only
    python tools/ota_push.py --version 1.1 --alpha 0.012 --target f05

    # Single room
    python tools/ota_push.py --version 1.1 --alpha 0.012 --target b01-f05-r505

    # Tamper test (wrong signature — engine should reject and raise alert)
    python tools/ota_push.py --version 1.1 --alpha 0.012 --tamper

Supported params (any subset):
    --alpha          float  thermal leakage constant
    --beta           float  HVAC strength
    --occupancy-heat float  heat added per occupied tick
    --humidity-gain  float  humidity correlation with temp delta
    --default-humidity float  nominal indoor humidity (%)
    --humidity-pull  float  per-tick pull toward nominal humidity
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time


def _compute_signature(params: dict) -> str:
    """SHA-256 over canonical JSON of the params dict (sort_keys=True)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_topic(prefix: str, target: str) -> str:
    """Return the correct OTA MQTT topic for the given target string."""
    target = target.strip().lower()
    if target == "all":
        return f"{prefix}/ota/config"
    # Floor: "f05" or "f5"
    if len(target) <= 3 and target.startswith("f"):
        try:
            floor_id = int(target[1:])
            return f"{prefix}/f{floor_id:02d}/ota"
        except ValueError:
            pass
    # Room: "b01-f05-r505"
    parts = target.split("-")
    if len(parts) == 3:
        try:
            floor_part = parts[1]   # fNN
            room_part = parts[2]    # rXXX
            floor_id = int(floor_part[1:])
            room_code = int(room_part[1:])
            return f"{prefix}/f{floor_id:02d}/r{room_code}/ota"
        except (ValueError, IndexError):
            pass
    print(
        f"WARNING: Could not parse target {target!r} as 'all', 'fNN', or 'b01-fNN-rXXX'. "
        "Defaulting to broadcast topic.",
        file=sys.stderr,
    )
    return f"{prefix}/ota/config"


def push_ota(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    prefix: str,
    version: str,
    params: dict,
    target: str,
    tamper: bool,
    qos: int,
) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt is required: pip install paho-mqtt", file=sys.stderr)
        sys.exit(1)

    topic = _build_topic(prefix, target)
    signature = _compute_signature(params)

    if tamper:
        # Corrupt the signature to test tamper detection in the engine
        signature = signature[:-4] + "XXXX"
        print(f"[TAMPER TEST] Signature corrupted — engine should reject this update.")

    payload = {
        "version": version,
        "params": params,
        "target": target,
        "signature": signature,
        "published_at": int(time.time()),
    }
    payload_json = json.dumps(payload, sort_keys=False)

    print(f"OTA push:")
    print(f"  broker  : {host}:{port}")
    print(f"  topic   : {topic}")
    print(f"  version : {version}")
    print(f"  target  : {target}")
    print(f"  params  : {json.dumps(params)}")
    print(f"  sig     : {signature[:32]}...")
    print(f"  tamper  : {tamper}")
    print()

    result_container = {"rc": None}

    def on_connect(client, userdata, flags, rc, props=None):
        result_container["rc"] = rc
        if rc == 0:
            info = client.publish(topic, payload_json, qos=qos, retain=False)
            info.wait_for_publish(timeout=10)
            print(f"Published {len(payload_json)} bytes to {topic!r} (QoS {qos})")
        else:
            print(f"Connection failed: rc={rc}", file=sys.stderr)
        client.disconnect()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"ota-push-{int(time.time())}",
    )
    if username:
        client.username_pw_set(username, password)
    client.on_connect = on_connect

    try:
        client.connect(host, port, keepalive=10)
        client.loop_forever()
    except OSError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        sys.exit(1)

    if result_container["rc"] != 0:
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="Phase 3 OTA push tool")
    p.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1885")))
    p.add_argument("--username", default=os.environ.get("MQTT_USER", ""))
    p.add_argument("--password", default=os.environ.get("MQTT_PASSWORD", ""))
    p.add_argument(
        "--prefix",
        default=os.environ.get("MQTT_TOPIC_PREFIX", "campus/b01"),
        help="Topic prefix (default: campus/b01)",
    )
    p.add_argument("--version", required=True, help='Firmware/config version string, e.g. "1.1"')
    p.add_argument(
        "--target",
        default="all",
        help='Target: "all" | "f05" | "b01-f05-r505" (default: all)',
    )
    p.add_argument("--alpha", type=float, help="Thermal leakage constant (alpha)")
    p.add_argument("--beta", type=float, help="HVAC strength (beta)")
    p.add_argument("--occupancy-heat", type=float, help="Heat added per occupied tick")
    p.add_argument("--humidity-gain", type=float, help="Humidity correlation with temp delta")
    p.add_argument("--default-humidity", type=float, help="Nominal indoor humidity (%)")
    p.add_argument("--humidity-pull", type=float, help="Per-tick pull toward nominal humidity")
    p.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])
    p.add_argument(
        "--tamper",
        action="store_true",
        help="Corrupt the signature to test tamper detection (engine should reject)",
    )

    args = p.parse_args()

    # Build params dict from provided CLI flags
    params: dict = {}
    if args.alpha is not None:
        params["alpha"] = args.alpha
    if args.beta is not None:
        params["beta"] = args.beta
    if args.occupancy_heat is not None:
        params["occupancy_heat"] = args.occupancy_heat
    if args.humidity_gain is not None:
        params["humidity_gain"] = args.humidity_gain
    if args.default_humidity is not None:
        params["default_humidity"] = args.default_humidity
    if args.humidity_pull is not None:
        params["humidity_pull"] = args.humidity_pull

    if not params:
        print("ERROR: At least one physics parameter must be specified (--alpha, --beta, ...).", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    push_ota(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        prefix=args.prefix,
        version=args.version,
        params=params,
        target=args.target,
        tamper=args.tamper,
        qos=args.qos,
    )


if __name__ == "__main__":
    main()
