from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Optional

import aiosqlite

from .room import Room


ROOM_STATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS room_states (
    room_id TEXT PRIMARY KEY,
    last_temp REAL,
    last_humidity REAL,
    hvac_mode TEXT,
    target_temp REAL,
    lighting_dimmer INTEGER,
    occupancy INTEGER,
    light_level INTEGER,
    last_update INTEGER
);
"""


class Persistence:
    """
    Persistence worker for Room state.

    - Restores last known truth on startup.
    - Periodically bulk-syncs the latest state.
    - Can be forced to create a “save point” upon receiving actuator commands.
    """

    def __init__(
        self,
        *,
        db_path: str,
        rooms_total_expected: int,
        sync_interval_sec: float,
    ):
        self._db_path = db_path
        self._rooms_total_expected = int(rooms_total_expected)
        self._sync_interval_sec = float(sync_interval_sec)

        self._sync_event = asyncio.Event()

    def request_sync(self) -> None:
        self._sync_event.set()

    async def init_db(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(ROOM_STATES_TABLE_SQL)
            await db.commit()

    async def load_room_states(self) -> Dict[str, dict]:
        """
        Load all room states from DB.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM room_states;") as cur:
                rows = await cur.fetchall()

        out: Dict[str, dict] = {}
        for r in rows:
            out[str(r["room_id"])] = dict(r)
        return out

    async def bulk_sync(self, rooms: Iterable[Room]) -> None:
        rooms_list = list(rooms)
        if not rooms_list:
            return

        insert_sql = """
        INSERT INTO room_states
            (room_id, last_temp, last_humidity, hvac_mode, target_temp, lighting_dimmer, occupancy, light_level, last_update)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(room_id) DO UPDATE SET
            last_temp=excluded.last_temp,
            last_humidity=excluded.last_humidity,
            hvac_mode=excluded.hvac_mode,
            target_temp=excluded.target_temp,
            lighting_dimmer=excluded.lighting_dimmer,
            occupancy=excluded.occupancy,
            light_level=excluded.light_level,
            last_update=excluded.last_update;
        """

        params = [r.db_tuple() for r in rooms_list]
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.executemany(insert_sql, params)
            await db.commit()

    async def run_sync_loop(
        self,
        *,
        rooms_by_id: Dict[str, Room],
        stop_event: asyncio.Event,
        log_fn,
    ) -> None:
        """
        Periodically sync the full fleet snapshot.

        log_fn(msg: str) is injected so we can keep this module dependency-free.
        """
        expected = self._rooms_total_expected
        while not stop_event.is_set():
            try:
                # Wait for either an immediate sync request or the interval timeout.
                self._sync_event.clear()
                try:
                    await asyncio.wait_for(self._sync_event.wait(), timeout=self._sync_interval_sec)
                    log_fn("persistence.savepoint requested")
                except asyncio.TimeoutError:
                    # Periodic save point.
                    pass

                # Snapshot all current room objects.
                await self.bulk_sync(list(rooms_by_id.values()))

                if len(rooms_by_id) != expected:
                    log_fn(f"persistence.synced_rooms={len(rooms_by_id)} expected={expected}")
            except Exception as e:
                log_fn(f"persistence.sync_error={type(e).__name__}:{e}")

        # Final sync on shutdown.
        try:
            await self.bulk_sync(list(rooms_by_id.values()))
        except Exception:
            pass

