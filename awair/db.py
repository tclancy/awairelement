"""SQLite connection, PRAGMAs, and idempotent schema bootstrap.

Schema changes: add a guarded migration in `_migrate()` — never edit the CREATE
statements for deployed columns, because `CREATE TABLE IF NOT EXISTS` leaves a
live table untouched and the edit would then describe a database that does not
exist.

Migrations guard on the *schema itself* (`PRAGMA table_info`), not on a version
counter. `PRAGMA user_version` is still 0 on the homelab after four schema
changes, so a counter here would have to be back-filled by guessing which
migrations a live DB had already had — and the schema can simply be asked.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# UTC-aware sentinel for fan_state rows we've never written — must be tz-aware
# so callers can subtract it from `datetime.now(timezone.utc)` without a
# naive/aware TypeError.
_NEVER = datetime(1970, 1, 1, tzinfo=UTC)

# How many dropped unkeyed rows `_migrate_outdoor_ts_not_null` spells out in the
# journal before summarising the rest. Zero is expected (#98).
_UNKEYED_LOG_CAP = 20

# The outdoor column list, defined once because two places create this table:
# `SCHEMA` on a fresh install, and `_migrate_outdoor_ts_not_null`'s rebuild on a
# deployed one. Retyping it in the migration is how the two installs drift, and
# `test_a_migrated_table_has_the_same_column_order_as_a_fresh_one` would only
# catch that after the fact -- one definition means the drift cannot be typed.
#
# `ts TEXT NOT NULL PRIMARY KEY` (#98). The NOT NULL is not redundant: SQLite
# keeps a longstanding compatibility bug where a non-INTEGER PRIMARY KEY on a
# rowid table gets no implicit NOT NULL, so two null-`ts` rows both insert and
# the table accumulates rows no dedup and no `ORDER BY ts` can key on.
#
# `weather_code` and `aq_ts` are ordered last, and in this order, to match what
# `_migrate` ALTERs onto an existing DB (#77). A fresh install and a migrated
# one otherwise end up with different physical column orders and SCHEMA stops
# describing a live database. Harmless while every query names its columns --
# which `test_schema_column_order_matches_a_migrated_database` keeps true --
# but SCHEMA is read as documentation, so it should not be false.
#
# Keep prose *outside* this string. SQLite stores the statement text verbatim
# and re-parses it on ALTER TABLE ... DROP COLUMN; a comment between the columns
# is left dangling by the drop and the ALTER fails with "incomplete input".
OUTDOOR_COLUMNS_DDL = """
    ts TEXT NOT NULL PRIMARY KEY,
    received_at TEXT NOT NULL,
    temp REAL, humid REAL, wind_speed REAL, pressure REAL, precipitation REAL,
    pm25 REAL, pm10 REAL, us_aqi INTEGER, co REAL, o3 REAL,
    weather_code INTEGER,
    aq_ts TEXT
"""

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
    last_command_at TEXT NOT NULL,
    run_started_at TEXT,
    capped INTEGER NOT NULL DEFAULT 0
);
"""
# Concatenated rather than interpolated: `str.format` and f-strings both treat
# `{` as a placeholder, and SQL is full of braceable syntax (a future CHECK or a
# JSON default would silently become a KeyError at import time).
SCHEMA += (
    "\nCREATE TABLE IF NOT EXISTS outdoor_readings (" + OUTDOOR_COLUMNS_DDL + ");\n"
)

OUTDOOR_COLUMNS = (
    "ts",
    "received_at",
    "temp",
    "humid",
    "wind_speed",
    "pressure",
    "precipitation",
    # Open-Meteo's WMO weather interpretation code (#71). Stored as the integer
    # the source published, not as a word: awairelement is the system of record
    # and the hub owns presentation, exactly as it owns card colour for indoor
    # events. See the `weather_code` entry in GLOSSARY.md for the full call.
    "weather_code",
    "pm25",
    "pm10",
    "us_aqi",
    "co",
    "o3",
    # The air-quality block's OWN observation time (#71) -- hourly, and so
    # routinely older than `ts`, which is the weather block's. Not the primary
    # key and not a substitute for `received_at`. See GLOSSARY.md: `aq_ts`.
    "aq_ts",
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
    # #71. Both nullable with no DEFAULT, which is the whole point: rows written
    # before this migration genuinely do not know their weather code or the age
    # of their AQI, and NULL is the only honest way to say so. Backfilling
    # either one -- even from the row's own `ts` -- would manufacture a
    # measurement time the source never published, and `aq_ts` exists precisely
    # so a consumer can refuse to trust an AQI it cannot date.
    _add_column(conn, "outdoor_readings", "weather_code INTEGER")
    _add_column(conn, "outdoor_readings", "aq_ts TEXT")
    # The duration cap's bookkeeping (ADR-002). `run_started_at` is nullable and
    # starts NULL: a fan already running at migration time has no recorded
    # start, and `run_exhausted` treats "no start" as "not yet exhausted", so
    # the first poll after deploy adopts it into a fresh run rather than
    # capping it instantly on a start time we never observed.
    _add_column(conn, "fan_state", "run_started_at TEXT")
    _add_column(conn, "fan_state", "capped INTEGER NOT NULL DEFAULT 0")
    # Last, and it must stay last: the rebuild copies the columns named in
    # OUTDOOR_COLUMNS, so it has to run after the ALTERs that add them. On a
    # pre-#71 DB the reverse order fails on "no such column: weather_code".
    _migrate_outdoor_ts_not_null(conn)


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


def _outdoor_ts_is_nullable(conn) -> bool:
    """Ask the live schema, rather than a version counter, what shape it is in."""
    for row in conn.execute("PRAGMA table_info(outdoor_readings)"):
        if row[1] == "ts":
            return not row[3]
    raise sqlite3.OperationalError("outdoor_readings has no `ts` column")


def _rebuild_outdoor_readings(conn) -> list:
    """Retype `outdoor_readings` with a NOT NULL `ts`. Returns the rows dropped.

    SQLite has no ALTER COLUMN, so tightening a constraint means the documented
    12-step rebuild. Caller owns the transaction.

    Unkeyed rows cannot come across: `ts` is the dedup key and the sort key, and
    a row without one is not a reading of any particular moment. Deriving one
    from `received_at` would manufacture an observation time Open-Meteo never
    published -- precisely what `_migrate`'s `aq_ts` comment refuses to do for
    the auxiliary clock. So they are dropped, and *returned*, so the caller can
    put them in the journal before the table that held them stops existing.
    """
    columns = ", ".join(OUTDOOR_COLUMNS)
    surplus = [
        row[1]
        for row in conn.execute("PRAGMA table_info(outdoor_readings)")
        if row[1] not in OUTDOOR_COLUMNS
    ]
    if surplus:
        # The rebuild copies the columns it knows about, so anything else goes
        # over the side silently. No such column has ever existed here, but this
        # is a DROP TABLE against the only copy of the record -- an unrecognised
        # column is a sign the DB is not the one this code was written for, and
        # refusing is recoverable where dropping is not.
        raise sqlite3.OperationalError(
            "refusing to rebuild outdoor_readings: it carries column(s) "
            f"{surplus} that this version of db.py does not know about, and "
            "the rebuild would drop them. Add them to OUTDOOR_COLUMNS first."
        )
    unkeyed = conn.execute(
        f"SELECT {columns} FROM outdoor_readings WHERE ts IS NULL"
    ).fetchall()
    # Idempotent even though the CREATE is inside the caller's transaction and
    # a crash therefore rolls it back: a leftover scratch table would make
    # `connect()` raise forever, taking both pollers and every web request down
    # with no self-heal, and one word removes the whole class.
    conn.execute("DROP TABLE IF EXISTS outdoor_readings_rebuilt")
    conn.execute(f"CREATE TABLE outdoor_readings_rebuilt ({OUTDOOR_COLUMNS_DDL})")
    conn.execute(
        f"INSERT INTO outdoor_readings_rebuilt ({columns})"
        f" SELECT {columns} FROM outdoor_readings WHERE ts IS NOT NULL"
    )
    conn.execute("DROP TABLE outdoor_readings")
    conn.execute("ALTER TABLE outdoor_readings_rebuilt RENAME TO outdoor_readings")
    return unkeyed


def _migrate_outdoor_ts_not_null(conn) -> None:
    """Close the nullable-PRIMARY-KEY hole on a deployed DB (#98).

    The deployed table is `ts TEXT PRIMARY KEY`, which SQLite does *not* make
    NOT NULL, so it accepts unlimited null-`ts` rows -- each one invisible to
    the dedup that column exists for and sorting first under every `ORDER BY
    ts`.

    `BEGIN IMMEDIATE` plus a re-check inside it, because poller and web both
    call `connect()` after restart.sh and a check-then-rebuild would let both
    processes rebuild. Taking the write lock up front makes the loser wait
    (`busy_timeout` is already 5s) and then find the work done. This is the same
    race `_add_column` tolerates by swallowing "duplicate column"; a rebuild has
    no equivalent idempotent verb, so it needs the lock.
    """
    if not _outdoor_ts_is_nullable(conn):
        return
    # `_migrate` issues only DDL today, and pysqlite does not implicitly BEGIN
    # for DDL -- so there is no open transaction here and this commit is a
    # no-op. It is here for the migration after next: the natural shape for one
    # is a backfill UPDATE, pysqlite *does* implicitly BEGIN for that, and
    # `BEGIN IMMEDIATE` inside a transaction raises "cannot start a transaction
    # within a transaction". `connect()` is called per web request as well as by
    # both pollers, so that failure takes down every surface at once with no
    # self-heal. The "must stay last" note above guards column order, not this.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _outdoor_ts_is_nullable(conn):
            conn.rollback()  # the other process rebuilt while we waited
            return
        dropped = _rebuild_outdoor_readings(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if dropped:
        log.warning(
            "outdoor_readings: dropped %d unkeyed row(s) with a NULL ts while"
            " migrating the column to NOT NULL; a reading with no source"
            " timestamp cannot be dated and was not backfilled (#98)",
            len(dropped),
        )
        # And the rows themselves. Their `received_at` and sensor values are
        # real observations -- only their source clock is missing -- so the
        # journal is the one recovery path once the table is gone. The expected
        # count is zero and the plausible failure is a handful, so this is
        # capped rather than paginated.
        for row in dropped[:_UNKEYED_LOG_CAP]:
            log.warning("outdoor_readings: dropped unkeyed row %r", row)
        if len(dropped) > _UNKEYED_LOG_CAP:
            log.warning(
                "outdoor_readings: %d further unkeyed row(s) not logged",
                len(dropped) - _UNKEYED_LOG_CAP,
            )


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
    poll wall-clock. The conflict clause makes the poll loop idempotent: if
    the upstream hasn't refreshed since the previous poll (the weather
    endpoint publishes every 15 min), the second write is a no-op.

    `ON CONFLICT(ts) DO NOTHING` rather than `INSERT OR IGNORE`, and the
    difference is load-bearing now that `ts` is NOT NULL (#98). `OR IGNORE`
    ignores *every* constraint violation, so a null `ts` would come back as
    rowcount 0 and this function would report it as a duplicate -- the one
    status that means "nothing is wrong". Tightening the column without also
    naming the conflict target would have converted #98's silent-garbage half
    into #95's silent-duplicate half rather than closing it. Naming `ts` keeps
    the dedup and lets a NOT NULL violation raise, where `poll_once` reports it
    honestly as an error.

    `insert_reading` (indoor) gets the same treatment in PR #97, which is open
    and is NOT an ancestor of this branch -- so on `main` the two pollers
    disagree until both land, with outdoor the tolerant one. #97 first, or at
    least alongside; the shared-`AWAIR_DB` argument above runs in both
    directions.
    """
    placeholders = ", ".join(f":{col}" for col in OUTDOOR_COLUMNS)
    try:
        cursor = conn.execute(
            f"INSERT INTO outdoor_readings ({', '.join(OUTDOOR_COLUMNS)})"
            f" VALUES ({placeholders}) ON CONFLICT(ts) DO NOTHING",
            reading,
        )
    except sqlite3.Error:
        # sqlite3 opens an implicit transaction before an INSERT and a raising
        # statement does not resolve it, so without this the connection sits
        # holding the write lock until some later poll commits. Both pollers
        # share one AWAIR_DB, so an outdoor payload fault would stall the
        # *indoor* writer too -- verified: a second connection gets "database
        # is locked" until the rollback lands.
        conn.rollback()
        raise
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


def latest_outdoor_reading(conn, columns) -> dict | None:
    """Most recent outdoor reading for `columns`, or None when none are stored.

    The outdoor sibling of `latest_reading`, and unbounded for the same reason
    (#70, #71): a consumer that wants to know whether the outdoor poller is
    dead needs "old row" and "no row" to arrive as different answers, and a
    `since` filter collapses them into one null.

    Ordered by `ts` DESC, which for this table is Open-Meteo's `current.time`
    -- the source's publish clock, not ours. That ordering is lexicographic,
    since `ts` is TEXT.

    **Mixed spellings are not the hazard here.** Rows predating the
    normalisation in `outdoor._normalize_source_time` hold a bare
    `"YYYY-MM-DDTHH:MM"`, and those still sort chronologically against
    normalised rows because the shared `YYYY-MM-DDTHH:MM` date prefix
    dominates the comparison; a bare value sorts early only against a
    normalised value denoting the same instant, where either answer is right.
    (`outdoor_readings_since`'s *range filter* is a different comparison and
    genuinely does need the normalisation -- see that docstring. Do not
    "fix" this one with a backfill.)

    The invariant that does need protecting is that `ts` is always UTC. That
    is a property of `outdoor._normalize_source_time`, and the reasoning moved
    onto it in #77 -- this function can neither cause nor fix it.

    Note this deliberately does NOT order by `received_at`. If Open-Meteo
    republishes a stale `current.time`, the older source reading is the correct
    answer to "what is the latest outdoor observation" even though our row for
    it landed later.
    """
    unknown = set(columns) - set(OUTDOOR_COLUMNS)
    if unknown:
        raise ValueError(f"unknown outdoor columns {unknown}")
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM outdoor_readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return dict(zip(columns, row, strict=True)) if row else None


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


def latest_reading(conn, columns) -> dict | None:
    """Most recent reading for `columns`, or None when the table is empty.

    **Deliberately unbounded, unlike `latest_pm25` / `latest_co2`.** Those two
    take a `since` because they gate spending fans, where a stale value must not
    be allowed to authorize or veto a turn-on — returning None is the safe
    answer there. This one feeds a read-only consumer that does its own
    staleness arithmetic (#70), and bounding it would destroy the distinction
    that consumer exists to draw: an old reading and no reading would both
    arrive as null, so "the poller died four hours ago" would be
    indistinguishable from "this house has never had a sensor". Hand back what
    is there, stamped with when it arrived, and let the caller judge it.

    Ordered by `ts` to match `idx_readings_ts` and every other query in this
    module. That is the device's clock, so a device whose clock runs fast pins
    itself at the top until real time catches up — which is exactly why the
    caller is given `received_at` as well and told to run staleness off it.
    The ordering is also *lexicographic*, since `ts` is stored as text: it
    equals chronological order only because every value is the Awair local
    API's fixed-width UTC `...Z` spelling, which `iso_z` exists to match. A row
    stored with a numeric offset would sort into the wrong place here and in
    `readings_since`, `metric_history`, `latest_co2` and `latest_pm25` alike.
    """
    unknown = set(columns) - set(READING_COLUMNS)
    if unknown:
        raise ValueError(f"unknown columns {unknown}")
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return dict(zip(columns, row, strict=True)) if row else None


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


def latest_co2(conn, since) -> float | None:
    """Most recent co2 reading at or after `since`, or None.

    Bounded by `since` for the same reason as `latest_pm25`: co2 is now the
    only thing that turns fans *on* (ADR-002), so a stale value must not keep them
    running through a sensor outage. None reads as 'no data' and the caller
    declines to act rather than guessing.
    """
    row = conn.execute(
        "SELECT co2 FROM readings"
        " WHERE ts >= ? AND co2 IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (iso_z(since),),
    ).fetchone()
    return float(row[0]) if row else None


def get_fan_state(conn, fan_id: int) -> dict:
    """Last-known state for one fan, defaulted to 'off' if never set.

    Never-set defaults use a distant-past command timestamp so the rate limit
    is not blocking on first use.
    """
    row = conn.execute(
        "SELECT last_action, last_command_at, run_started_at, capped"
        " FROM fan_state WHERE fan_id = ?",
        (fan_id,),
    ).fetchone()
    if row is None:
        return {
            "fan_id": fan_id,
            "last_action": "off",
            "last_command_at": _NEVER,
            "run_started_at": None,
            "capped": False,
        }
    last_action, last_command_at, run_started_at, capped = row
    return {
        "fan_id": fan_id,
        "last_action": last_action,
        "last_command_at": datetime.fromisoformat(last_command_at),
        "run_started_at": (
            datetime.fromisoformat(run_started_at) if run_started_at else None
        ),
        "capped": bool(capped),
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


def set_fan_run(conn, fan_id: int, started_at, capped: bool) -> None:
    """Persist the duration cap's bookkeeping for one fan (ADR-002).

    Deliberately separate from `upsert_fan_state`: the cap has to be recorded
    on polls where *no command is owed*. Clearing `capped` is the case that
    forces this — it happens while the fans are already off and co2 has
    finally recovered, so `decide()` returns None and nothing else would write.
    Folding this into upsert_fan_state would tie it to commands actually sent
    and the flag would never clear.

    Touches only the run columns, so a failed actuate's "keep the old
    last_action" contract is unaffected.
    """
    conn.execute(
        "INSERT INTO fan_state (fan_id, last_action, last_command_at,"
        " run_started_at, capped) VALUES (?, 'off', ?, ?, ?)"
        " ON CONFLICT(fan_id) DO UPDATE SET"
        " run_started_at = excluded.run_started_at,"
        " capped = excluded.capped",
        (
            fan_id,
            _NEVER.isoformat(),
            started_at.isoformat() if started_at else None,
            int(capped),
        ),
    )
    conn.commit()
