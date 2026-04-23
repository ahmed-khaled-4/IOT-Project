"""Idempotent ThingsBoard provisioning script for Phase 2.

This script talks to the local ThingsBoard REST API and creates:

  * Device Profiles:
        - MQTT-ThermalSensor
        - CoAP-ThermalSensor
        - Campus-Gateway-MQTT (DEFAULT + MQTT transport for tb-gateway; CE has no GATEWAY profile type)
  * Asset hierarchy:
        Campus (b01) -> Building b01 -> Floor fNN -> Room rXXX (x200)
  * Devices:
        200 devices, one per Room. 100 MQTT + 100 CoAP.
  * Relations:
        Campus -Contains-> Building -Contains-> Floor -Contains-> Room
        Room -Contains-> Device
  * Access Tokens:
        Read back per device and dumped to devices.csv so the engine
        can authenticate.
  * Thermal profiles: declarative HighTemp alarm + shared ``highTempThreshold`` (CE root chain)
  * Gateway bridge: device profile + gateway device for ThingsBoard IoT
    Gateway (HiveMQ → TB). The gateway device is created with
    additionalInfo.gateway=true so CE accepts v1/gateway/* MQTT.
    Platform MQTT integrations are PE-only; CE uses tb-gateway plus
    infra/thingsboard/tb-gateway/*.json.

Usage:

    # Once the docker-compose stack is up:
    python infra/thingsboard/bootstrap.py \\
        --url http://localhost:9090 \\
        --user tenant@thingsboard.org \\
        --password tenant

The script is safe to re-run; entities are looked up by name first
and only created if missing.

``--touch-telemetry`` posts REST timeseries for every room device, then
publishes one HiveMQ message per *CoAP* room so ``tb-gateway`` ingests
it— otherwise CE can keep CoAP profile devices "Inactive" until
gateway-delivered data arrives (compose: hivemq on host 1885, tb-gateway up).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

BUILDING_ID = "b01"
FLOORS = list(range(1, 11))          # 01..10
ROOMS_PER_FLOOR = 20
MQTT_ROOM_INDEX_RANGE = (1, 10)       # r{f}01..r{f}10
COAP_ROOM_INDEX_RANGE = (11, 20)      # r{f}11..r{f}20

DEVICE_PROFILE_MQTT = "MQTT-ThermalSensor"
DEVICE_PROFILE_COAP = "CoAP-ThermalSensor"
DEVICE_PROFILE_GATEWAY = "Campus-Gateway-MQTT"
GATEWAY_DEVICE_NAME = "Campus-HiveMQ-Gateway"
DEFAULT_GATEWAY_ACCESS_TOKEN = "phase2-campus-tb-gateway-token-change-me"

ROOT_ASSET_NAME = "Campus"


def _coap_touch_mqtt_publishes(
    rows: List[Dict[str, str]],
    *,
    now_ms: int,
    now_s: int,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    """One QoS-1 PUBLISH per CoAP room so tb-gateway can forward to ThingsBoard.

    Room JSON matches engine/mqtt.json mapping (``sensor_id``, ``tb_profile``,
    ``ts``/``timestamp``).
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:  # pragma: no cover
        print(
            "touch-telemetry: install paho-mqtt so CoAP devices can be marked Active via HiveMQ "
            "( `pip install paho-mqtt` ). Skipping MQTT touch for coap room devices."
        )
        return
    coap_rows = [r for r in rows if r.get("device_type") == "coap"]
    if not coap_rows:
        return
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bootstrap_coap_touch")
    client.username_pw_set(username, password)
    try:
        client.connect(host, port, keepalive=30)
    except OSError as e:
        print(
            f"touch-telemetry: could not connect to HiveMQ at {host}:{port} ({e!r}). "
            "Is docker compose up (hivemq, tb-gateway)? CoAP devices may stay Inactive in TB."
        )
        return
    client.loop_start()
    n_ok = 0
    for row in coap_rows:
        topic = f"campus/b01/{row['floor']}/r{row['room_code']}/telemetry"
        body = {
            "sensor_id": row["room_id"],
            "tb_profile": DEVICE_PROFILE_COAP,
            "temperature": 22.0,
            "humidity": 45.0,
            "occupancy": False,
            "light_level": 400,
            "hvac_mode": "ECO",
            "lighting_dimmer": 60,
            "timestamp": now_s,
            "ts": now_ms,
            "tb_ts_ms": now_ms,
        }
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        pr = client.publish(topic, payload.encode("utf-8"), qos=1, retain=False)
        try:
            pr.wait_for_publish()
            n_ok += 1
        except Exception:  # pragma: no cover
            print(f"touch-telemetry: MQTT publish failed for {topic!r}.")
    client.loop_stop()
    client.disconnect()
    print(f"touch-telemetry: HiveMQ CoAP pre-touch published for {n_ok}/{len(coap_rows)} room topics.")

# Server-side attribute (ms). ThingsBoard default is often 10 min; longer window avoids
# "Inactive" in the UI when telemetry is sparse or CoAP paths are still starting.
DEFAULT_DEVICE_INACTIVITY_TIMEOUT_MS = 30 * 24 * 60 * 60 * 1000

# Declarative alarm on the device profile (ThingsBoard root rule chain evaluates these
# via the "Device profile" node — no custom rule chain assignment required on CE).
# Clear when temperature <= threshold. Matches dashboard / runbook type "HighTemp".
_THERMAL_HIGH_TEMP_ALARM: Dict[str, Any] = {
    "id": "phase2-high-temp",
    "alarmType": "HighTemp",
    "createRules": {
        "MAJOR": {
            "condition": {
                "condition": [
                    {
                        "key": {"type": "TIME_SERIES", "key": "temperature"},
                        "valueType": "NUMERIC",
                        "predicate": {
                            "type": "NUMERIC",
                            "operation": "GREATER",
                            "value": {"defaultValue": 30.0},
                        },
                    }
                ],
                "spec": {"type": "SIMPLE"},
            }
        }
    },
    "clearRule": {
        "condition": {
            "condition": [
                {
                    "key": {"type": "TIME_SERIES", "key": "temperature"},
                    "valueType": "NUMERIC",
                    "predicate": {
                        "type": "NUMERIC",
                        "operation": "LESS_OR_EQUAL",
                        "value": {"defaultValue": 30.0},
                    },
                }
            ],
            "spec": {"type": "SIMPLE"},
        }
    },
    "propagate": True,
    "propagateToOwner": False,
    "propagateToTenant": False,
}


class TBClient:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.token: Optional[str] = None
        self.sess = requests.Session()

    def login(self) -> None:
        r = self.sess.post(
            f"{self.url}/api/auth/login",
            json={"username": self.user, "password": self.password},
            timeout=30,
        )
        r.raise_for_status()
        self.token = r.json()["token"]
        self.sess.headers.update({"X-Authorization": f"Bearer {self.token}"})

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        r = self.sess.request(method, f"{self.url}{path}", timeout=60, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"TB {method} {path} -> {r.status_code} {r.text[:300]}")
        return r

    # ----- lookup helpers -----

    def find_device(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            r = self._req("GET", f"/api/tenant/devices?deviceName={name}")
            return r.json()
        except RuntimeError:
            return None

    def find_asset(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            r = self._req("GET", f"/api/tenant/assets?assetName={name}")
            return r.json()
        except RuntimeError:
            return None

    def find_device_profile(self, name: str) -> Optional[Dict[str, Any]]:
        r = self._req("GET", "/api/deviceProfiles?pageSize=200&page=0")
        for p in r.json().get("data", []):
            if p.get("name") == name:
                return p
        return None

    # ----- creation helpers -----

    def create_device_profile(self, name: str, description: str) -> Dict[str, Any]:
        existing = self.find_device_profile(name)
        if existing:
            return existing
        body = {
            "name": name,
            "description": description,
            "type": "DEFAULT",
            "transportType": "DEFAULT",
            "provisionType": "DISABLED",
            "profileData": {
                "configuration": {"type": "DEFAULT"},
                "transportConfiguration": {"type": "DEFAULT"},
                "provisionConfiguration": {"type": "DISABLED"},
                "alarms": [dict(_THERMAL_HIGH_TEMP_ALARM)],
            },
        }
        r = self._req("POST", "/api/deviceProfile", json=body)
        return r.json()

    def create_gateway_device_profile(self, name: str, description: str) -> Dict[str, Any]:
        """CE only accepts DeviceProfileType.DEFAULT (GATEWAY is PE-only)."""
        existing = self.find_device_profile(name)
        if existing:
            return existing
        body = {
            "name": name,
            "description": description,
            "type": "DEFAULT",
            "transportType": "MQTT",
            "provisionType": "DISABLED",
            "default": False,
            "profileData": {
                "configuration": {"type": "DEFAULT"},
                "transportConfiguration": {
                    "type": "MQTT",
                    "deviceTelemetryTopic": "v1/devices/me/telemetry",
                    "deviceAttributesTopic": "v1/devices/me/attributes",
                    "deviceAttributesSubscribeTopic": "v1/devices/me/attributes",
                    "deviceRpcRequestTopic": "v1/devices/me/rpc/request/$request_id",
                    "deviceRpcResponseTopic": "v1/devices/me/rpc/response/$request_id",
                    "connectTimeoutSec": 10,
                },
                "provisionConfiguration": {"type": "DISABLED"},
                "alarms": [],
            },
        }
        r = self._req("POST", "/api/deviceProfile", json=body)
        return r.json()

    def set_device_access_token(self, device: Dict[str, Any], token: str) -> None:
        """Create or overwrite ACCESS_TOKEN credentials (POST without `id` only works once)."""
        did = device["id"]
        dev_uuid = did["id"] if isinstance(did, dict) else str(did)
        device_id_obj = did if isinstance(did, dict) else {"id": dev_uuid, "entityType": "DEVICE"}
        body: Dict[str, Any] = {
            "deviceId": device_id_obj,
            "credentialsType": "ACCESS_TOKEN",
            "credentialsId": token,
        }
        r = self.sess.get(f"{self.url}/api/device/{dev_uuid}/credentials", timeout=60)
        if r.status_code == 200:
            existing = r.json()
            if existing and existing.get("id"):
                body["id"] = existing["id"]
        elif r.status_code != 404:
            raise RuntimeError(f"TB GET /api/device/{dev_uuid}/credentials -> {r.status_code} {r.text[:300]}")
        self._req("POST", "/api/device/credentials", json=body)

    def create_asset(self, name: str, type_: str, label: str = "") -> Dict[str, Any]:
        existing = self.find_asset(name)
        if existing:
            return existing
        body = {"name": name, "type": type_, "label": label or name}
        r = self._req("POST", "/api/asset", json=body)
        return r.json()

    def create_device(
        self,
        name: str,
        profile_id: str,
        label: str = "",
        additional_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing = self.find_device(name)
        if existing:
            return existing
        body: Dict[str, Any] = {
            "name": name,
            "label": label or name,
            "deviceProfileId": {"id": profile_id, "entityType": "DEVICE_PROFILE"},
        }
        if additional_info is not None:
            body["additionalInfo"] = additional_info
        r = self._req("POST", "/api/device", json=body)
        return r.json()

    def ensure_device_gateway_mqtt_mode(self, device_name: str) -> None:
        """Enable MQTT Gateway API (v1/gateway/*) for this device (CE uses additionalInfo, not profile type)."""
        dev = self.find_device(device_name)
        if not dev or not dev.get("id"):
            raise RuntimeError(f"Device not found: {device_name}")
        did = dev["id"]["id"] if isinstance(dev["id"], dict) else dev["id"]
        full = self._req("GET", f"/api/device/{did}").json()
        info: Dict[str, Any] = dict(full.get("additionalInfo") or {})
        if info.get("gateway") is True:
            return
        info["gateway"] = True
        full["additionalInfo"] = info
        self._req("POST", "/api/device", json=full)
        print(
            f"gateway device {device_name!r}: set additionalInfo.gateway=true "
            f"(required for ThingsBoard to accept IoT Gateway telemetry)"
        )

    def get_device_access_token(self, device_id: str) -> str:
        r = self._req("GET", f"/api/device/{device_id}/credentials")
        data = r.json()
        return data.get("credentialsId") or ""

    def create_relation(self, from_id: str, from_type: str, to_id: str, to_type: str) -> None:
        body = {
            "from": {"id": from_id, "entityType": from_type},
            "to": {"id": to_id, "entityType": to_type},
            "type": "Contains",
            "typeGroup": "COMMON",
        }
        self._req("POST", "/api/relation", json=body)

    def set_device_server_attributes(self, device_uuid: str, attributes: Dict[str, Any]) -> None:
        """SERVER_SCOPE attributes (e.g. inactivityTimeout in ms — see TB device connectivity docs)."""
        self._req(
            "POST",
            f"/api/plugins/telemetry/DEVICE/{device_uuid}/attributes/SERVER_SCOPE",
            json=attributes,
        )

    def post_device_telemetry(self, device_uuid: str, values: Dict[str, Any], ts_ms: int) -> None:
        """Tenant-admin upload of time-series (marks device as having activity)."""
        self._req(
            "POST",
            f"/api/plugins/telemetry/DEVICE/{device_uuid}/timeseries/ANY",
            json={"ts": ts_ms, "values": values},
        )

    def ensure_thermal_profile_alarms_and_threshold(self, profile: Dict[str, Any]) -> None:
        """Ensure MQTT/CoAP thermal profiles have HighTemp alarm rules + shared highTempThreshold.

        Existing tenants created before this logic ran only had empty ``alarms`` arrays;
        re-run bootstrap to patch profiles in place.
        """
        pid = profile["id"]["id"]
        self._req(
            "POST",
            f"/api/plugins/telemetry/DEVICE_PROFILE/{pid}/SHARED_SCOPE",
            json={"highTempThreshold": 30.0},
        )
        full = self._req("GET", f"/api/deviceProfile/{pid}").json()
        pdata = full.setdefault("profileData", {})
        alarms = [a for a in (pdata.get("alarms") or []) if a.get("id") != "phase2-high-temp"]
        alarms.append(dict(_THERMAL_HIGH_TEMP_ALARM))
        pdata["alarms"] = alarms
        self._req("POST", "/api/deviceProfile", json=full)
        print(
            f"thermal profile OK: name={profile.get('name')!r} "
            f"HighTemp declarative (MAJOR if temperature > 30) + shared highTempThreshold=30"
        )


def _room_code(floor: int, idx: int) -> int:
    return floor * 100 + idx


def _room_asset_name(floor: int, idx: int) -> str:
    return f"{BUILDING_ID}-f{floor:02d}-r{_room_code(floor, idx)}"


def _device_name(floor: int, idx: int) -> str:
    return _room_asset_name(floor, idx)


def provision(
    url: str,
    user: str,
    password: str,
    out_csv: str,
    *,
    device_inactivity_timeout_ms: int,
    touch_telemetry: bool,
    touch_mqtt_coap: bool,
    touch_mqtt_host: str,
    touch_mqtt_port: int,
    touch_mqtt_user: str,
    touch_mqtt_password: str,
) -> None:
    tb = TBClient(url, user, password)
    tb.login()

    profile_mqtt = tb.create_device_profile(
        DEVICE_PROFILE_MQTT, "MQTT thermal sensor device profile (Phase 2)"
    )
    profile_coap = tb.create_device_profile(
        DEVICE_PROFILE_COAP, "CoAP thermal sensor device profile (Phase 2)"
    )
    print(f"device profiles OK: {profile_mqtt['name']}, {profile_coap['name']}")
    tb.ensure_thermal_profile_alarms_and_threshold(profile_mqtt)
    tb.ensure_thermal_profile_alarms_and_threshold(profile_coap)

    campus = tb.create_asset(ROOT_ASSET_NAME, type_="Campus", label="Phase 2 Campus")
    building = tb.create_asset(BUILDING_ID, type_="Building", label="Building 01")
    tb.create_relation(campus["id"]["id"], "ASSET", building["id"]["id"], "ASSET")

    csv_rows: List[Dict[str, str]] = []

    for floor in FLOORS:
        floor_name = f"f{floor:02d}"
        floor_asset = tb.create_asset(floor_name, type_="Floor", label=f"Floor {floor:02d}")
        tb.create_relation(building["id"]["id"], "ASSET", floor_asset["id"]["id"], "ASSET")

        for idx in range(1, ROOMS_PER_FLOOR + 1):
            room_name = _room_asset_name(floor, idx)
            room_asset = tb.create_asset(room_name, type_="Room", label=room_name)
            tb.create_relation(floor_asset["id"]["id"], "ASSET", room_asset["id"]["id"], "ASSET")

            if MQTT_ROOM_INDEX_RANGE[0] <= idx <= MQTT_ROOM_INDEX_RANGE[1]:
                profile = profile_mqtt
                kind = "mqtt"
            else:
                profile = profile_coap
                kind = "coap"

            dev = tb.create_device(
                name=_device_name(floor, idx),
                profile_id=profile["id"]["id"],
                label=f"{kind.upper()} Thermal Sensor {room_name}",
            )
            tb.create_relation(room_asset["id"]["id"], "ASSET", dev["id"]["id"], "DEVICE")
            dev_uuid = dev["id"]["id"] if isinstance(dev["id"], dict) else str(dev["id"])
            if device_inactivity_timeout_ms > 0:
                tb.set_device_server_attributes(
                    dev_uuid, {"inactivityTimeout": int(device_inactivity_timeout_ms)}
                )
            token = tb.get_device_access_token(dev_uuid)
            csv_rows.append(
                {
                    "room_id": room_name,
                    "floor": floor_name,
                    "room_code": str(_room_code(floor, idx)),
                    "device_type": kind,
                    "profile": profile["name"],
                    "access_token": token,
                }
            )

        print(f"floor {floor_name}: 20 devices provisioned")

    if touch_telemetry:
        import time as _time

        now_ms = int(_time.time() * 1000)
        print(
            "touch-telemetry: posting one sample per room device so ThingsBoard marks them active "
            "(engine traffic will overwrite with live values)."
        )
        for row in csv_rows:
            dev_name = row["room_id"]
            found = tb.find_device(dev_name)
            if not found or not found.get("id"):
                continue
            du = found["id"]["id"] if isinstance(found["id"], dict) else str(found["id"])
            tb.post_device_telemetry(
                du,
                {
                    "temperature": 22.0,
                    "humidity": 45.0,
                    "occupancy": False,
                    "light_level": 400,
                    "hvac_mode": "ECO",
                    "lighting_dimmer": 60,
                },
                now_ms,
            )
        now_s = int(now_ms // 1000)
        if touch_mqtt_coap:
            _coap_touch_mqtt_publishes(
                csv_rows,
                now_ms=now_ms,
                now_s=now_s,
                host=touch_mqtt_host,
                port=touch_mqtt_port,
                username=touch_mqtt_user,
                password=touch_mqtt_password,
            )

    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "room_id",
                "floor",
                "room_code",
                "device_type",
                "profile",
                "access_token",
            ],
        )
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"wrote {out_csv} ({len(csv_rows)} devices)")

    gw_token = os.environ.get("TB_GATEWAY_ACCESS_TOKEN", DEFAULT_GATEWAY_ACCESS_TOKEN)
    gw_profile = tb.create_gateway_device_profile(
        DEVICE_PROFILE_GATEWAY,
        "ThingsBoard IoT Gateway — HiveMQ campus MQTT bridge (Phase 2)",
    )
    gw_dev = tb.create_device(
        name=GATEWAY_DEVICE_NAME,
        profile_id=gw_profile["id"]["id"],
        label="Campus HiveMQ → ThingsBoard bridge (tb-gateway container)",
        additional_info={"gateway": True},
    )
    tb.set_device_access_token(gw_dev, gw_token)
    tb.ensure_device_gateway_mqtt_mode(GATEWAY_DEVICE_NAME)
    print(
        f"gateway bridge OK: device={GATEWAY_DEVICE_NAME!r} profile={DEVICE_PROFILE_GATEWAY!r} "
        f"(rotate with TB_GATEWAY_ACCESS_TOKEN env + docker compose; default matches tb-gateway service)"
    )
    print("If tb-gateway is already running, restart it so the MQTT session picks up gateway mode:")
    print("  docker compose restart tb-gateway")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("TB_URL", "http://localhost:9090"))
    p.add_argument("--user", default=os.environ.get("TB_USER", "tenant@thingsboard.org"))
    p.add_argument("--password", default=os.environ.get("TB_PASSWORD", "tenant"))
    p.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(__file__), "devices.csv"),
        help="Where to write the device credentials CSV",
    )
    p.add_argument(
        "--device-inactivity-timeout-ms",
        type=int,
        default=DEFAULT_DEVICE_INACTIVITY_TIMEOUT_MS,
        help="SERVER_SCOPE inactivityTimeout per room device (0 = skip). Default: 30 days.",
    )
    p.add_argument(
        "--touch-telemetry",
        action="store_true",
        help="Post one sample per room via TB REST, then publish CoAP rooms to HiveMQ so all 200 show Active (needs hivemq+tb-gateway).",
    )
    p.add_argument(
        "--skip-mqtt-coap-touch",
        action="store_true",
        help="With --touch-telemetry, only use REST (CoAP room devices may stay Inactive in the TB UI on CE).",
    )
    p.add_argument(
        "--touch-mqtt-host",
        default=os.environ.get("TB_TOUCH_MQTT_HOST", "127.0.0.1"),
        help="HiveMQ host for CoAP pre-touch (default: 127.0.0.1 or TB_TOUCH_MQTT_HOST).",
    )
    p.add_argument(
        "--touch-mqtt-port",
        type=int,
        default=int(os.environ.get("TB_TOUCH_MQTT_PORT", "1885")),
        help="HiveMQ port on host (compose maps 1885->1883; default 1885 or TB_TOUCH_MQTT_PORT).",
    )
    p.add_argument(
        "--touch-mqtt-user",
        default=os.environ.get("TB_TOUCH_MQTT_USER", "thingsboard"),
    )
    p.add_argument(
        "--touch-mqtt-password",
        default=os.environ.get("TB_TOUCH_MQTT_PASSWORD", "changeit"),
    )
    args = p.parse_args()

    try:
        provision(
            args.url,
            args.user,
            args.password,
            args.out,
            device_inactivity_timeout_ms=args.device_inactivity_timeout_ms,
            touch_telemetry=args.touch_telemetry,
            touch_mqtt_coap=not args.skip_mqtt_coap_touch,
            touch_mqtt_host=args.touch_mqtt_host,
            touch_mqtt_port=args.touch_mqtt_port,
            touch_mqtt_user=args.touch_mqtt_user,
            touch_mqtt_password=args.touch_mqtt_password,
        )
    except requests.exceptions.ConnectionError:
        print(
            "ThingsBoard is not reachable at " + args.url + ". Start the stack with `docker compose up`.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
