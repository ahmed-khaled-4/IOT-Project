"""Phase 3 ThingsBoard UI provisioning: imports Rule Chain + Dashboard via REST API.

Deletes any existing Phase 3 rule chain / dashboard (by name), then re-imports
fresh copies from the JSON files in this directory.  After import it assigns
the new rule chain to all relevant device profiles so that telemetry flows
through the Phase 3 processing pipeline (Floor Tag, Save TS, OTA tamper alarm).

Usage:
    .venv\\Scripts\\python infra/thingsboard/import_phase3.py
    .venv\\Scripts\\python infra/thingsboard/import_phase3.py --url http://localhost:9090

Safe to re-run at any time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RC_JSON = os.path.join(SCRIPT_DIR, "rule-chain-phase3.json")
DASH_JSON = os.path.join(SCRIPT_DIR, "dashboard-phase3.json")

RC_NAME = "Phase 3 - Digital Twin & OTA"
DASH_NAME = "Phase 3 - Digital Twin Dashboard"

DEVICE_PROFILES_TO_ASSIGN = [
    "Campus-Room-MQTT",
    "Campus-Room-CoAP",
    "MQTT-ThermalSensor",
    "CoAP-ThermalSensor",
    "Campus-Gateway-MQTT",
]


class TBApi:
    def __init__(self, base_url: str, user: str, password: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self._login(user, password)

    def _login(self, user: str, password: str) -> None:
        r = self.session.post(
            f"{self.base}/api/auth/login",
            json={"username": user, "password": password},
        )
        r.raise_for_status()
        token = r.json()["token"]
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        print(f"[OK] Logged in as {user}")

    def _get(self, path: str, **kwargs) -> Any:
        r = self.session.get(f"{self.base}{path}", **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: Any) -> Any:
        r = self.session.post(f"{self.base}{path}", json=payload)
        if not r.ok:
            print(f"  [ERR] POST {path} -> {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        r = self.session.delete(f"{self.base}{path}")
        r.raise_for_status()

    # --- Rule Chains ---

    def list_rule_chains(self) -> List[Dict]:
        data = self._get("/api/ruleChains", params={"pageSize": 100, "page": 0})
        return data.get("data", [])

    def delete_rule_chain(self, rc_id: str) -> None:
        self._delete(f"/api/ruleChain/{rc_id}")

    def import_rule_chain(self, rc_json: Dict) -> Dict:
        """Create rule chain shell, then upload metadata (nodes + connections)."""
        rc_body = {
            "name": rc_json["ruleChain"]["name"],
            "type": rc_json["ruleChain"].get("type", "CORE"),
            "root": rc_json["ruleChain"].get("root", False),
            "debugMode": rc_json["ruleChain"].get("debugMode", False),
            "configuration": rc_json["ruleChain"].get("configuration"),
        }
        created = self._post("/api/ruleChain", rc_body)
        rc_id = created["id"]["id"]
        print(f"  [OK] Created rule chain shell: {rc_id}")

        meta = rc_json.get("metadata", {})
        clean_nodes = []
        for node in meta.get("nodes", []):
            clean = {k: v for k, v in node.items() if k not in ("id", "createdTime", "ruleChainId", "externalId")}
            clean_nodes.append(clean)

        meta_payload = {
            "ruleChainId": {"entityType": "RULE_CHAIN", "id": rc_id},
            "firstNodeIndex": meta.get("firstNodeIndex", 0),
            "nodes": clean_nodes,
            "connections": meta.get("connections", []),
            "ruleChainConnections": meta.get("ruleChainConnections", []),
        }
        r = self.session.post(f"{self.base}/api/ruleChain/metadata", json=meta_payload)
        if not r.ok:
            print(f"  [ERR] metadata POST -> {r.status_code}: {r.text[:500]}")
            r.raise_for_status()
        print(f"  [OK] Uploaded {len(clean_nodes)} nodes + {len(meta.get('connections', []))} connections")
        return created

    def set_root_rule_chain(self, rc_id: str) -> None:
        r = self.session.post(f"{self.base}/api/ruleChain/{rc_id}/root")
        r.raise_for_status()

    # --- Device Profiles ---

    def list_device_profiles(self) -> List[Dict]:
        data = self._get("/api/deviceProfiles", params={"pageSize": 100, "page": 0})
        return data.get("data", [])

    def get_device_profile(self, dp_id: str) -> Dict:
        return self._get(f"/api/deviceProfile/{dp_id}")

    def save_device_profile(self, dp: Dict) -> Dict:
        return self._post("/api/deviceProfile", dp)

    # --- Dashboards ---

    def list_dashboards(self) -> List[Dict]:
        data = self._get("/api/tenant/dashboards", params={"pageSize": 100, "page": 0})
        return data.get("data", [])

    def delete_dashboard(self, dash_id: str) -> None:
        self._delete(f"/api/dashboard/{dash_id}")

    def create_dashboard(self, dash_json: Dict) -> Dict:
        clean = {k: v for k, v in dash_json.items() if k not in ("id", "createdTime", "tenantId", "externalId", "assignedCustomers")}
        return self._post("/api/dashboard", clean)


def run(url: str, user: str, password: str) -> None:
    api = TBApi(url, user, password)

    # ---- 1. Delete existing Phase 3 rule chain (if any) ----
    print("\n--- Rule Chain ---")
    existing_rcs = api.list_rule_chains()
    old_rc_id = None
    for rc in existing_rcs:
        if rc["name"] == RC_NAME:
            old_rc_id = rc["id"]["id"]
            is_root = rc.get("root", False)
            if is_root:
                fallback = [r for r in existing_rcs if r["name"] != RC_NAME and r.get("root") is False]
                if fallback:
                    api.set_root_rule_chain(fallback[0]["id"]["id"])
                    print(f"  [OK] Moved root to '{fallback[0]['name']}' before deletion")
            # Unassign from any device profiles that reference this chain
            for dp_summary in api.list_device_profiles():
                dp_full = api.get_device_profile(dp_summary["id"]["id"])
                chain_ref = dp_full.get("defaultRuleChainId")
                if chain_ref and chain_ref.get("id") == old_rc_id:
                    dp_full["defaultRuleChainId"] = None
                    api.save_device_profile(dp_full)
                    print(f"  [OK] Unassigned '{dp_summary['name']}' from old chain")
            api.delete_rule_chain(old_rc_id)
            print(f"  [OK] Deleted old rule chain: {old_rc_id}")

    # ---- 2. Import new rule chain ----
    with open(RC_JSON, "r", encoding="utf-8") as f:
        rc_payload = json.load(f)

    created_rc = api.import_rule_chain(rc_payload)
    rc_id = created_rc["id"]["id"]
    print(f"  [OK] Imported rule chain: {rc_id}")

    # ---- 3. Assign to device profiles + add alarm rule for gateway ----
    print("\n--- Device Profile Assignment ---")
    profiles = api.list_device_profiles()
    for dp_summary in profiles:
        dp_name = dp_summary.get("name", "")
        if dp_name not in DEVICE_PROFILES_TO_ASSIGN:
            continue
        dp_full = api.get_device_profile(dp_summary["id"]["id"])
        dp_full["defaultRuleChainId"] = {"entityType": "RULE_CHAIN", "id": rc_id}

        if dp_name == "Campus-Gateway-MQTT":
            profile_data = dp_full.get("profileData", {})
            alarm_rule = {
                "id": "ota_tamper_alarm",
                "alarmType": "Security Tampering Alert",
                "createRules": {
                    "CRITICAL": {
                        "schedule": None,
                        "condition": {
                            "condition": [
                                {
                                    "key": {
                                        "type": "TIME_SERIES",
                                        "key": "ota_tamper"
                                    },
                                    "valueType": "BOOLEAN",
                                    "value": True,
                                    "predicate": {
                                        "type": "BOOLEAN",
                                        "operation": "EQUAL",
                                        "value": {
                                            "defaultValue": True,
                                            "dynamicValue": None
                                        }
                                    }
                                }
                            ],
                            "spec": {
                                "type": "SIMPLE"
                            }
                        },
                        "alarmDetails": "OTA tamper detected. Check ota_topic and ota_received_sig telemetry for details."
                    }
                },
                "propagate": True,
                "propagateToOwner": True,
                "propagateToTenant": True
            }
            existing_alarms = profile_data.get("alarms", [])
            existing_alarms = [a for a in existing_alarms if a.get("id") != "ota_tamper_alarm"]
            existing_alarms.append(alarm_rule)
            profile_data["alarms"] = existing_alarms
            dp_full["profileData"] = profile_data
            print(f"  [OK] Added OTA tamper alarm rule to {dp_name}")

        api.save_device_profile(dp_full)
        print(f"  [OK] {dp_name} -> Phase 3 rule chain")

    # ---- 4. Delete existing Phase 3 dashboard (if any) ----
    print("\n--- Dashboard ---")
    existing_dashes = api.list_dashboards()
    for d in existing_dashes:
        if d.get("title") == DASH_NAME or d.get("name") == DASH_NAME:
            api.delete_dashboard(d["id"]["id"])
            print(f"  [OK] Deleted old dashboard: {d['id']['id']}")

    # ---- 5. Import new dashboard ----
    with open(DASH_JSON, "r", encoding="utf-8") as f:
        dash_payload = json.load(f)

    created_dash = api.create_dashboard(dash_payload)
    dash_id = created_dash["id"]["id"]
    print(f"  [OK] Imported dashboard: {dash_id}")
    print(f"  [OK] Open: {url}/dashboards/{dash_id}")

    print("\n=== Phase 3 UI provisioning complete ===")
    print(f"  Rule Chain : {url}/ruleChains/{rc_id}")
    print(f"  Dashboard  : {url}/dashboards/{dash_id}")
    print()
    print("Next steps (manual in ThingsBoard UI):")
    print("  1. Open the dashboard and verify all 4 widgets load data")
    print("  2. To add Image Map heatmap: edit dashboard -> Add Widget -> Maps -> Image Map")
    print("     Upload your floor plan image, add entity alias 'Floor 01 Rooms', draw room polygons")
    print("  3. Run a tamper test to verify the alarm widget populates:")
    print("     .venv\\Scripts\\python tools/ota_push.py --host localhost --port 1885 --version 1.2 --alpha 0.01 --target all --tamper")


def main():
    p = argparse.ArgumentParser(description="Import Phase 3 rule chain + dashboard into ThingsBoard")
    p.add_argument("--url", default=os.environ.get("TB_URL", "http://localhost:9090"))
    p.add_argument("--user", default=os.environ.get("TB_USER", "tenant@thingsboard.org"))
    p.add_argument("--password", default=os.environ.get("TB_PASSWORD", "tenant"))
    args = p.parse_args()
    run(args.url, args.user, args.password)


if __name__ == "__main__":
    main()
