"""SQLite connection, PRAGMAs, and idempotent schema bootstrap.

Schema changes: bump PRAGMA user_version and add a guarded migration in
connect() — never edit the CREATE statements for deployed columns.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    score INTEGER, temp REAL, humid REAL, abs_humid REAL, dew_point REAL,
    co2 INTEGER, co2_est INTEGER, co2_est_baseline INTEGER,
    voc INTEGER, voc_baseline INTEGER, voc_h2_raw INTEGER, voc_ethanol_raw INTEGER,
    pm25 REAL, pm10_est INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY,
    metric TEXT NOT NULL,
    tier TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    peak_value REAL, baseline REAL, threshold REAL,
    open_notified INTEGER NOT NULL DEFAULT 0,
    close_notified INTEGER NOT NULL DEFAULT 0,
    renotified_at TEXT
);
"""

READING_COLUMNS = (
    "ts", "received_at",
    "score", "temp", "humid", "abs_humid", "dew_point",
    "co2", "co2_est", "co2_est_baseline",
    "voc", "voc_baseline", "voc_h2_raw", "voc_ethanol_raw",
    "pm25", "pm10_est",
)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


def insert_reading(conn: sqlite3.Connection, reading: dict) -> bool:
    """Insert one reading. False if the device timestamp is already stored."""
    placeholders = ", ".join(f":{col}" for col in READING_COLUMNS)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO readings ({', '.join(READING_COLUMNS)})"
        f" VALUES ({placeholders})",
        reading,
    )
    conn.commit()
    return cursor.rowcount == 1
