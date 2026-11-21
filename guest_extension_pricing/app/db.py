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
""")

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
