"""SQLite storage. One connection, guarded by a lock; the app is mostly single-threaded asyncio."""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY,
    hostaway_listing_id INTEGER UNIQUE,
    hostaway_name TEXT,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    lock_automation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS locks (
    id INTEGER PRIMARY KEY,
    entity_id TEXT NOT NULL UNIQUE,
    name TEXT,
    state TEXT,
    property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    match_source TEXT,
    code_hashes TEXT,
    code_names TEXT,
    codes_read_at TEXT,
    codes_error TEXT,
    seen_at TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    guest_name TEXT,
    status TEXT NOT NULL,
    active INTEGER NOT NULL,
    arrival_date TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    check_in_at TEXT NOT NULL,
    check_out_at TEXT NOT NULL,
    door_code TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_listing ON reservations(listing_id, check_in_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    property_id INTEGER,
    reservation_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS guest_notices (
    reservation_id INTEGER PRIMARY KEY,
    property_id INTEGER,
    sent_at TEXT NOT NULL,
    delivered INTEGER NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    key TEXT PRIMARY KEY,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alert_recipients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    target TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS preview_codes (
    id INTEGER PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL,   -- UTC, same format as next_check_at; the lock logic removes the code after this
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thermostats (
    id INTEGER PRIMARY KEY,
    entity_id TEXT NOT NULL UNIQUE,
    name TEXT,
    state TEXT,              -- HVAC mode (heat, cool, heat_cool, auto, off) or unavailable
    property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    match_source TEXT,
    current_temp REAL,
    target_temp REAL,
    target_low REAL,
    target_high REAL,
    hvac_action TEXT,
    fan_mode TEXT,
    offline_since TEXT,
    last_mode TEXT,          -- occupied | vacant: what we last set successfully
    last_applied_at TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    seen_at TEXT
);
"""

# Columns added after the first release; applied to existing databases on start.
MIGRATIONS = {
    "thermostats": {
        "min_temp": "REAL",        # the range Home Assistant says the device accepts
        "max_temp": "REAL",
    },
    "reservations": {
        "override_code": "TEXT",   # a code staff typed for the guest; replaces Hostaway's and survives syncs
    },
    "properties": {
        "backup_code": "TEXT",
        "backup_used_by": "INTEGER",
        "address": "TEXT",
        "thermostat_automation": "INTEGER NOT NULL DEFAULT 0",
    },
    "locks": {
        "battery": "INTEGER",
        "next_check_at": "TEXT",
        "fail_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
        "last_reconciled_at": "TEXT",
    },
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                for column, ddl in columns.items():
                    if column not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).lastrowid

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.get_setting(key, "1" if default else "0") == "1"

    def log(self, kind: str, message: str, *, level: str = "info",
            property_id: int | None = None, reservation_id: int | None = None) -> None:
        self.execute(
            "INSERT INTO events(at, level, kind, property_id, reservation_id, message) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (utcnow(), level, kind, property_id, reservation_id, message),
        )
