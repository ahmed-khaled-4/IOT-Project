"""Phase 3 ThingsBoard provisioning: Digital Twin hierarchy + server attributes + shadow state.

Creates / ensures:
  ZC-Main-Campus  (type: campus)
    B01           (type: building)
      B01-F01 .. B01-F10  (type: floor)
        B01-F01-R001 .. B01-F01-R020  (type: room)  [20 per floor = 200 rooms]

Links existing Phase 2 devices (named b01-fNN-rXXX) to their corresponding room assets.

Sets on each room ASSET (SERVER_SCOPE attributes):
  - square_footage   (int)
  - occupant_capacity (int)
  - coordinates_x    (int)  — pixel X on a 900×560 floor-plan grid (5 cols × 4 rows)
  - coordinates_y    (int)  — pixel Y
  - room_type        (str)  — "lecture_hall" | "lab" | "office" | "corridor"

Sets on each room DEVICE (SHARED_SCOPE attributes — initial shadow state):
  - desired_hvac_mode       = "ECO"
  - desired_lighting_dimmer = 60
  - version                 = "1.0"   (OTA target version; engine client attr tracks current_version)

Usage:
    python infra/thingsboard/bootstrap_phase3.py \\
        --url http://localhost:9090 \\
        --user tenant@thingsboard.org \\
        --password tenant

Safe to re-run; assets and relations are looked up by name first.
Run Phase 2 bootstrap (bootstrap.py) before this script so the 200 devices already exist.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Re-use TBClient from Phase 2 bootstrap (same directory)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))
from bootstrap import TBClient  # noqa: E402


# ---------------------------------------------------------------------------
# Phase 3 constants
# ---------------------------------------------------------------------------

FLOORS = list(range(1, 11))
ROOMS_PER_FLOOR = 20

P3_CAMPUS_NAME = "ZC-Main-Campus"
P3_CAMPUS_TYPE = "campus"

P3_BUILDING_NAME = "B01"
P3_BUILDING_TYPE = "building"
P3_BUILDING_LABEL = "Building 01"

FLOOR_TYPE = "floor"
ROOM_TYPE_ASSET = "room"

# Room metadata seeded deterministically by index-on-floor (1–20)
# Indices 1-5: lecture_hall, 6-10: lab, 11-15: office, 16-20: corridor
_ROOM_TYPE_MAP = {
    range(1, 6): ("lecture_hall", 95, 60),
    range(6, 11): ("lab", 55, 25),
    range(11, 16): ("office", 28, 12),
    range(16, 21): ("corridor", 18, 0),
}

# Grid layout for the floor plan: 5 columns × 4 rows, 180px wide × 140px tall per cell
_GRID_COLS = 5
_GRID_CELL_W = 180
_GRID_CELL_H = 140
_GRID_ORIGIN_X = 90   # centre of first cell
_GRID_ORIGIN_Y = 70


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def _floor_asset_name(floor: int) -> str:
    return f"B01-F{floor:02d}"


def _room_asset_name(floor: int, idx: int) -> str:
    return f"B01-F{floor:02d}-R{idx:03d}"


def _p2_device_name(floor: int, idx: int) -> str:
    """Phase 2 device name: b01-fNN-rXXX where XXX = floor*100+idx."""
    return f"b01-f{floor:02d}-r{floor * 100 + idx}"


def _room_meta(idx: int) -> Tuple[str, int, int]:
    """Return (room_type, square_footage, occupant_capacity) for index 1-20."""
    for rng, (rtype, sqft, cap) in _ROOM_TYPE_MAP.items():
        if idx in rng:
            return rtype, sqft, cap
    return "office", 28, 12


def _room_coordinates(idx: int) -> Tuple[int, int]:
    col = (idx - 1) % _GRID_COLS
    row = (idx - 1) // _GRID_COLS
    return (
        _GRID_ORIGIN_X + col * _GRID_CELL_W,
        _GRID_ORIGIN_Y + row * _GRID_CELL_H,
    )


def _room_server_attributes(idx: int) -> Dict[str, Any]:
    rtype, sqft, cap = _room_meta(idx)
    cx, cy = _room_coordinates(idx)
    return {
        "room_type": rtype,
        "square_footage": sqft,
        "occupant_capacity": cap,
        "coordinates_x": cx,
        "coordinates_y": cy,
    }


# ---------------------------------------------------------------------------
# Extended TBClient with Phase 3 helpers
# ---------------------------------------------------------------------------

class P3TBClient(TBClient):
    """Extends Phase 2 TBClient with asset attribute and device shared attribute methods."""

    def set_asset_server_attributes(self, asset_uuid: str, attributes: Dict[str, Any]) -> None:
        self._req(
            "POST",
            f"/api/plugins/telemetry/ASSET/{asset_uuid}/attributes/SERVER_SCOPE",
            json=attributes,
        )

    def set_device_shared_attributes(self, device_uuid: str, attributes: Dict[str, Any]) -> None:
        self._req(
            "POST",
            f"/api/plugins/telemetry/DEVICE/{device_uuid}/attributes/SHARED_SCOPE",
            json=attributes,
        )

    def find_or_create_asset(self, name: str, type_: str, label: str = "") -> Dict[str, Any]:
        """Idempotent: find by name or create."""
        return self.create_asset(name, type_=type_, label=label or name)

    def ensure_relation(
        self,
        from_id: str,
        from_type: str,
        to_id: str,
        to_type: str,
    ) -> None:
        """Create relation only if it does not already exist (best-effort idempotency)."""
        try:
            self.create_relation(from_id, from_type, to_id, to_type)
        except RuntimeError as e:
            if "already exist" in str(e).lower() or "duplicate" in str(e).lower():
                pass
            else:
                raise


# ---------------------------------------------------------------------------
# Provisioning logic
# ---------------------------------------------------------------------------

def provision(url: str, user: str, password: str, *, dry_run: bool = False) -> None:
    tb = P3TBClient(url, user, password)
    tb.login()
    print(f"Logged in to ThingsBoard at {url}")

    # 1. Root campus asset
    campus = tb.find_or_create_asset(P3_CAMPUS_NAME, type_=P3_CAMPUS_TYPE, label="ZC Main Campus")
    campus_uuid = campus["id"]["id"]
    print(f"[1/4] Campus asset OK: {P3_CAMPUS_NAME!r} (uuid={campus_uuid[:8]}...)")

    # 2. Building B01
    building = tb.find_or_create_asset(P3_BUILDING_NAME, type_=P3_BUILDING_TYPE, label=P3_BUILDING_LABEL)
    building_uuid = building["id"]["id"]
    tb.ensure_relation(campus_uuid, "ASSET", building_uuid, "ASSET")
    print(f"[2/4] Building asset OK: {P3_BUILDING_NAME!r}")

    # 3. Floors + room assets + server attributes + device links + shared attributes
    print(f"[3/4] Provisioning {len(FLOORS)} floors × {ROOMS_PER_FLOOR} rooms ...")
    total_rooms = 0
    total_linked = 0
    total_missing = 0

    for floor in FLOORS:
        floor_name = _floor_asset_name(floor)
        floor_asset = tb.find_or_create_asset(floor_name, type_=FLOOR_TYPE, label=f"Floor {floor:02d}")
        floor_uuid = floor_asset["id"]["id"]
        tb.ensure_relation(building_uuid, "ASSET", floor_uuid, "ASSET")

        for idx in range(1, ROOMS_PER_FLOOR + 1):
            room_name = _room_asset_name(floor, idx)
            room_asset = tb.find_or_create_asset(room_name, type_=ROOM_TYPE_ASSET, label=room_name)
            room_uuid = room_asset["id"]["id"]
            tb.ensure_relation(floor_uuid, "ASSET", room_uuid, "ASSET")

            # Server attributes on the room ASSET
            if not dry_run:
                tb.set_asset_server_attributes(room_uuid, _room_server_attributes(idx))

            # Link Phase 2 device to this room asset
            p2_name = _p2_device_name(floor, idx)
            p2_dev = tb.find_device(p2_name)
            if p2_dev and p2_dev.get("id"):
                dev_uuid = p2_dev["id"]["id"] if isinstance(p2_dev["id"], dict) else str(p2_dev["id"])
                tb.ensure_relation(room_uuid, "ASSET", dev_uuid, "DEVICE")

                # Initial shared attributes for shadow state + OTA tracking
                if not dry_run:
                    tb.set_device_shared_attributes(dev_uuid, {
                        "desired_hvac_mode": "ECO",
                        "desired_lighting_dimmer": 60,
                        "version": "1.0",
                    })
                total_linked += 1
            else:
                print(f"  WARNING: Phase 2 device {p2_name!r} not found — run bootstrap.py first")
                total_missing += 1

            total_rooms += 1

        print(f"  floor {floor_name}: {ROOMS_PER_FLOOR} room assets OK")

    print(
        f"[3/4] Done: {total_rooms} room assets, {total_linked} device links, "
        f"{total_missing} missing Phase-2 devices"
    )

    # 4. Summary
    print(f"\n[4/4] Phase 3 provisioning complete!")
    print(f"  Hierarchy: {P3_CAMPUS_NAME} -> {P3_BUILDING_NAME} -> B01-F01..B01-F10 -> B01-F0N-R001..R020")
    print(f"  Room server attributes:  room_type, square_footage, occupant_capacity, coordinates_x/y")
    print(f"  Device shared attributes: desired_hvac_mode, desired_lighting_dimmer, version")
    print()
    print("Next manual steps:")
    print("  1. ThingsBoard → Rule Chains → Import: infra/thingsboard/rule-chain-phase3.json")
    print("  2. ThingsBoard → Dashboards   → Import: infra/thingsboard/dashboard-phase3.json")
    print("  3. ThingsBoard → Image Gallery → Upload a floor-plan PNG")
    print("  4. Open the Phase 3 dashboard, edit Image Map widget, draw 20 room polygons per floor")
    print("  5. Restart tb-gateway: docker compose restart tb-gateway")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Phase 3 ThingsBoard provisioning: Digital Twin + shadow state init"
    )
    p.add_argument("--url", default=os.environ.get("TB_URL", "http://localhost:9090"))
    p.add_argument("--user", default=os.environ.get("TB_USER", "tenant@thingsboard.org"))
    p.add_argument("--password", default=os.environ.get("TB_PASSWORD", "tenant"))
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Create assets/relations but skip attribute writes (for testing connectivity)",
    )
    args = p.parse_args()

    try:
        provision(args.url, args.user, args.password, dry_run=args.dry_run)
    except requests.exceptions.ConnectionError:
        print(
            f"ThingsBoard is not reachable at {args.url}. "
            "Start the stack with `docker compose up`.",
            file=sys.stderr,
        )
        sys.exit(2)
    except Exception as e:
        print(f"Provisioning error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
