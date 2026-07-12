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
    renotified_at TEXT,
    notified_value REAL,
    fans_engaged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fan_state (
    fan_id INTEGER PRIMARY KEY,
    last_action TEXT NOT NULL CHECK (last_action IN ('off', 'speed1', 'speed2', 'speed3')),
    last_command_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outdoor_readings (
    ts TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    temp REAL, humid REAL, wind_speed REAL, pressure REAL, precipitation REAL,
    pm25 REAL, pm10 REAL, us_aqi INTEGER, co REAL, o3 REAL
);
"""

OUTDOOR_COLUMNS = (
    "ts",
    "received_at",
    "temp",
    "humid",
    "wind_speed",
    "pressure",
    "precipitation",
    "pm25",
    "pm10",
    "us_aqi",
    "co",
    "o3",
)

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
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """In-place additions for DBs created under an older SCHEMA.

    CREATE TABLE IF NOT EXISTS leaves existing tables untouched, so columns
    added to SCHEMA after first deploy need an explicit ALTER here.
    """
    _add_column(conn, "alert_events", "notified_value REAL")
    # The fan score gate's latch. DEFAULT 0 is load-bearing: a live DB has open
    # events at migration time, and they must land unlatched rather than
    # spuriously driving the fans on the first poll after deploy.
    _add_column(conn, "alert_events", "fans_engaged INTEGER NOT NULL DEFAULT 0")


def _add_column(conn, table: str, column_def: str) -> None:
    """ALTER ADD COLUMN that tolerates the column already existing.

    Poller and web both run connect() after restart.sh; a check-then-ALTER
    would let the loser of that race crash on "duplicate column name", so
    the ALTER is attempted unconditionally and duplicates read as success.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


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


def insert_outdoor_reading(conn: sqlite3.Connection, reading: dict) -> bool:
    """Insert one outdoor reading. False if this source-time is already stored.

    `ts` is Open-Meteo's `current.time` — the source publish time, not our
    poll wall-clock. INSERT OR IGNORE makes the poll loop idempotent: if
    the upstream hasn't refreshed since the previous poll (the weather
    endpoint publishes every 15 min), the second write is a no-op.
    """
    placeholders = ", ".join(f":{col}" for col in OUTDOOR_COLUMNS)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO outdoor_readings ({', '.join(OUTDOOR_COLUMNS)})"
        f" VALUES ({placeholders})",
        reading,
    )
    conn.commit()
    return cursor.rowcount == 1


def outdoor_readings_since(conn, columns, since) -> list:
    """[(epoch_seconds, col1, col2, ...)] ascending for outdoor columns."""
    unknown = set(columns) - set(OUTDOOR_COLUMNS)
    if unknown:
        raise ValueError(f"unknown outdoor columns {unknown}")
    rows = conn.execute(
        f"SELECT ts, {', '.join(columns)} FROM outdoor_readings"
        f" WHERE ts >= ? ORDER BY ts",
        (since.isoformat(),),
    )
    return [(datetime.fromisoformat(ts).timestamp(), *values) for ts, *values in rows]


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
        " baseline, threshold, notified_value, fans_engaged"
        " FROM alert_events WHERE closed_at IS NULL"
    )
    return {
        row[1]: {
            "id": row[0],
            "metric": row[1],
            "tier": row[2],
            "opened_at": datetime.fromisoformat(row[3]),
            "renotified_at": (datetime.fromisoformat(row[4]) if row[4] else None),
            "peak_value": row[5],
            "baseline": row[6],
            "threshold": row[7],
            "notified_value": row[8],
            "fans_engaged": row[9],
        }
        for row in rows
    }


def mark_fans_engaged(conn, event_id) -> None:
    """Latch this event as fan-worthy: the score dipped below the gate while it
    was open. Write-once — the score is never consulted again for this event."""
    conn.execute(
        "UPDATE alert_events SET fans_engaged = 1 WHERE id = ?",
        (event_id,),
    )
    conn.commit()


def open_event(
    conn, metric, tier, opened_at, value, baseline, threshold, notified
) -> int:
    cursor = conn.execute(
        "INSERT INTO alert_events"
        " (metric, tier, opened_at, peak_value, baseline, threshold,"
        "  open_notified, notified_value)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            metric,
            tier,
            opened_at.isoformat(),
            value,
            baseline,
            threshold,
            int(notified),
            value,
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


def mark_renotified(conn, event_id, at, value) -> None:
    """Record a mid-event notification; `value` re-arms escalation laddering."""
    conn.execute(
        "UPDATE alert_events SET renotified_at = ?, notified_value = ? WHERE id = ?",
        (at.isoformat(), value, event_id),
    )
    conn.commit()


def escalate_event(conn, event_id, at, value, tier) -> None:
    """Tier promotion and/or magnitude escalation: one notification, re-arm."""
    conn.execute(
        "UPDATE alert_events SET tier = ?, renotified_at = ?, notified_value = ?"
        " WHERE id = ?",
        (tier, at.isoformat(), value, event_id),
    )
    conn.commit()


def latest_pm25(conn, since) -> float | None:
    """Most recent pm25 reading at or after `since`, or None.

    Bounded by `since` because pm25 is used as a suppressor — a stale value
    (from an old cooking event, days ago) must not silently keep fans off.
    Returns None when the last read is older than the window; the caller
    treats that the same as 'no sensor data' rather than trusting old.
    """
    row = conn.execute(
        "SELECT pm25 FROM readings"
        " WHERE ts >= ? AND pm25 IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (iso_z(since),),
    ).fetchone()
    return float(row[0]) if row else None


def latest_score(conn, since) -> float | None:
    """Most recent Awair score at or after `since`, or None.

    Bounded by `since` for the same reason as `latest_pm25`: the score is a
    gate on spending fans, and a stale value must not authorize (or veto) a
    turn-on. Returns None when the last read is older than the window; the
    caller treats that as 'no data' and declines to engage.
    """
    row = conn.execute(
        "SELECT score FROM readings"
        " WHERE ts >= ? AND score IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (iso_z(since),),
    ).fetchone()
    return float(row[0]) if row else None


def get_fan_state(conn, fan_id: int) -> dict:
    """Last-known state for one fan, defaulted to 'off' if never set.

    Never-set defaults use a distant-past command timestamp so the rate limit
    is not blocking on first use.
    """
    row = conn.execute(
        "SELECT last_action, last_command_at FROM fan_state WHERE fan_id = ?",
        (fan_id,),
    ).fetchone()
    if row is None:
        return {
            "fan_id": fan_id,
            "last_action": "off",
            "last_command_at": _NEVER,
        }
    last_action, last_command_at = row
    return {
        "fan_id": fan_id,
        "last_action": last_action,
        "last_command_at": datetime.fromisoformat(last_command_at),
    }


def upsert_fan_state(conn, fan_id: int, action: str, command_at) -> None:
    """Persist last-known fan state.

    On a failed actuate the caller should pass the pre-existing action here
    (unchanged) but still stamp command_at — the rate limit doubles as a
    backoff so a broken NodeMCU is retried every RATE_LIMIT, not every poll.
    """
    conn.execute(
        "INSERT INTO fan_state (fan_id, last_action, last_command_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(fan_id) DO UPDATE SET"
        " last_action = excluded.last_action,"
        " last_command_at = excluded.last_command_at",
        (fan_id, action, command_at.isoformat()),
    )
    conn.commit()
