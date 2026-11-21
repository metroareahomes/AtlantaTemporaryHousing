"""Persistent settings, queue and nightly rates."""
import logging
import sqlite3


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extension_messages (
    id INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS extension_rates (
    listing_id INTEGER NOT NULL,
    night TEXT NOT NULL,
    rate TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    captured_on TEXT,
    snapshot_rate TEXT,
    restored_on TEXT,
    PRIMARY KEY (listing_id, night)
);
CREATE TABLE IF NOT EXISTS extension_price_windows (
    listing_id INTEGER NOT NULL,
    night TEXT NOT NULL,
    starts_at REAL NOT NULL,
    cutoff REAL NOT NULL,
    PRIMARY KEY (listing_id, night, cutoff)
);
CREATE TABLE IF NOT EXISTS extension_booking_events (
    reservation_id INTEGER NOT NULL,
    listing_id INTEGER NOT NULL,
    arrival TEXT NOT NULL,
    departure TEXT NOT NULL,
    guest_key TEXT,
    booked_at REAL NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (reservation_id, listing_id, arrival, departure, booked_at)
);
CREATE TABLE IF NOT EXISTS extension_booked_nights (
    listing_id INTEGER NOT NULL,
    night TEXT NOT NULL,
    reservation_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    net_rent TEXT NOT NULL,
    rent_increase TEXT NOT NULL,
    PRIMARY KEY (listing_id, night)
);
""")

        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(extension_rates)")}
        for name, sql_type in (
            ("tracking", "INTEGER NOT NULL DEFAULT 1"),
            ("base_rate", "TEXT"),
            ("reservation_id", "INTEGER"),
            ("guest_key", "TEXT"),
            ("use_stay_average", "INTEGER NOT NULL DEFAULT 0"),
            ("stay_average_rate", "TEXT"),
            ("owner", "TEXT"),
        ):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE extension_rates ADD COLUMN {name} {sql_type}")

    def query(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args)]

    def execute(self, sql, args=()):
        self.conn.execute(sql, args)

    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def log(self, kind, message, *, level="info", reservation_id=None):
        logging.getLogger(__name__).log(
            logging.WARNING if level == "warning" else logging.INFO, "%s %s", kind, message
        )
