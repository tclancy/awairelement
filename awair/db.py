"""SQLite connection, PRAGMAs, and idempotent schema bootstrap.

Schema changes: bump PRAGMA user_version and add a guarded migration in
connect() — never edit the CREATE statements for deployed columns.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# UTC-aware sentinel for fan_state rows we've never written — must be tz-aware
# so callers can subtract it from `datetime.now(timezone.utc)` without a
# naive/aware TypeError.
_NEVER = datetime(1970, 1, 1, tzinfo=timezone.utc)

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

CREATE TABLE IF NOT EXISTS fan_state (
    fan_id INTEGER PRIMARY KEY,
    last_action TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    last_command_at TEXT NOT NULL
);
"""

READING_COLUMNS = (
    "ts",
    "received_at",
    "score",
    "temp",
    "humid",
    "abs_humid",
    "dew_point",
    "co2",
    "co2_est",
    "co2_est_baseline",
    "voc",
    "voc_baseline",
    "voc_h2_raw",
    "voc_ethanol_raw",
    "pm25",
    "pm10_est",
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


def readings_since(conn, columns, since) -> list:
    """[(epoch_seconds, col1, col2, ...)] ascending for the given columns."""
    unknown = set(columns) - set(READING_COLUMNS)
    if unknown:
        raise ValueError(f"unknown columns {unknown}")
    rows = conn.execute(
        f"SELECT ts, {', '.join(columns)} FROM readings WHERE ts >= ? ORDER BY ts",
        (iso_z(since),),
    )
    return [(datetime.fromisoformat(ts).timestamp(), *values) for ts, *values in rows]


def events_since(conn, since) -> list:
    """Events overlapping [since, now]: closed within it, or still open."""
    rows = conn.execute(
        "SELECT metric, tier, opened_at, closed_at, peak_value, baseline, threshold"
        " FROM alert_events WHERE closed_at IS NULL OR closed_at >= ?"
        " ORDER BY opened_at",
        (since.isoformat(),),
    )
    return [
        {
            "metric": metric,
            "tier": tier,
            "opened_at": datetime.fromisoformat(opened_at).timestamp(),
            "closed_at": (
                datetime.fromisoformat(closed_at).timestamp() if closed_at else None
            ),
            "peak_value": peak,
            "baseline": baseline,
            "threshold": threshold,
        }
        for metric, tier, opened_at, closed_at, peak, baseline, threshold in rows
    ]


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


def open_event(
    conn, metric, tier, opened_at, value, baseline, threshold, notified
) -> int:
    cursor = conn.execute(
        "INSERT INTO alert_events"
        " (metric, tier, opened_at, peak_value, baseline, threshold, open_notified)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            metric,
            tier,
            opened_at.isoformat(),
            value,
            baseline,
            threshold,
            int(notified),
        ),
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


def latest_pm25(conn) -> float | None:
    """Most recent pm25 reading, or None if the table is empty / null."""
    row = conn.execute(
        "SELECT pm25 FROM readings WHERE pm25 IS NOT NULL ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return float(row[0]) if row else None


def get_fan_state(conn, fan_id: int) -> dict:
    """Last-known state for one fan, defaulted to 'off' if never set.

    Never-set defaults use a distant-past command timestamp so the rate limit
    is not blocking on first use.
    """
    row = conn.execute(
        "SELECT last_action, last_changed_at, last_command_at"
        " FROM fan_state WHERE fan_id = ?",
        (fan_id,),
    ).fetchone()
    if row is None:
        return {
            "fan_id": fan_id,
            "last_action": "off",
            "last_changed_at": _NEVER,
            "last_command_at": _NEVER,
        }
    last_action, last_changed_at, last_command_at = row
    return {
        "fan_id": fan_id,
        "last_action": last_action,
        "last_changed_at": datetime.fromisoformat(last_changed_at),
        "last_command_at": datetime.fromisoformat(last_command_at),
    }


def upsert_fan_state(conn, fan_id: int, action: str, changed_at, command_at) -> None:
    """Persist a fan transition. changed_at and command_at may be the same."""
    conn.execute(
        "INSERT INTO fan_state (fan_id, last_action, last_changed_at, last_command_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(fan_id) DO UPDATE SET"
        " last_action = excluded.last_action,"
        " last_changed_at = excluded.last_changed_at,"
        " last_command_at = excluded.last_command_at",
        (fan_id, action, changed_at.isoformat(), command_at.isoformat()),
    )
    conn.commit()
