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


def provision(url: str, user: str, password: str, out_csv: str) -> None:
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
            token = tb.get_device_access_token(dev["id"]["id"])
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
    args = p.parse_args()

    try:
        provision(args.url, args.user, args.password, args.out)
    except requests.exceptions.ConnectionError:
        print(
            "ThingsBoard is not reachable at " + args.url + ". Start the stack with `docker compose up`.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
