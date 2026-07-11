"""SQLite connection, PRAGMAs, and idempotent schema bootstrap.

Schema changes: bump PRAGMA user_version and add a guarded migration in
connect() — never edit the CREATE statements for deployed columns.
"""

import sqlite3
from datetime import datetime
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


def iso_z(dt) -> str:
    """UTC datetime → the device's timestamp format, so strings sort together."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def metric_history(conn, metric: str, since) -> list:
    """[(datetime, value)] ascending for one metric, nulls excluded."""
    if metric not in READING_COLUMNS:
        raise ValueError(f"unknown metric {metric!r}")
    rows = conn.execute(
        f"SELECT ts, {metric} FROM readings"
        f" WHERE ts >= ? AND {metric} IS NOT NULL ORDER BY ts",
        (iso_z(since),),
    )
    return [(datetime.fromisoformat(ts), float(v)) for ts, v in rows]


def get_open_events(conn) -> dict:
    """Open alert events keyed by metric (at most one open per metric)."""
    rows = conn.execute(
        "SELECT id, metric, tier, opened_at, renotified_at, peak_value,"
        " baseline, threshold FROM alert_events WHERE closed_at IS NULL"
    )
    return {
        metric: {
            "id": event_id,
            "metric": metric,
            "tier": tier,
            "opened_at": datetime.fromisoformat(opened_at),
            "renotified_at": (
                datetime.fromisoformat(renotified_at) if renotified_at else None
            ),
            "peak_value": peak,
            "baseline": baseline,
            "threshold": threshold,
        }
        for event_id, metric, tier, opened_at, renotified_at, peak, baseline, threshold in rows
    }


def open_event(conn, metric, tier, opened_at, value, baseline, threshold, notified) -> int:
    cursor = conn.execute(
        "INSERT INTO alert_events"
        " (metric, tier, opened_at, peak_value, baseline, threshold, open_notified)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (metric, tier, opened_at.isoformat(), value, baseline, threshold, int(notified)),
    )
    conn.commit()
    return cursor.lastrowid


def close_event(conn, event_id, closed_at, notified) -> None:
    conn.execute(
        "UPDATE alert_events SET closed_at = ?, close_notified = ? WHERE id = ?",
        (closed_at.isoformat(), int(notified), event_id),
    )
    conn.commit()


def update_peak(conn, event_id, value) -> None:
    conn.execute(
        "UPDATE alert_events SET peak_value = MAX(COALESCE(peak_value, ?), ?)"
        " WHERE id = ?",
        (value, value, event_id),
    )
    conn.commit()


def mark_renotified(conn, event_id, at) -> None:
    conn.execute(
        "UPDATE alert_events SET renotified_at = ? WHERE id = ?",
        (at.isoformat(), event_id),
    )
    conn.commit()
