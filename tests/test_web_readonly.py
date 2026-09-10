"""The web process bootstraps the schema once, then reads read-only (#73).

Two properties are under test and they fail differently, so they are asserted
separately:

* **No DDL per request.** An absence assertion, so every one of these carries a
  reachability control that proves the spy would have seen the DDL if it ran.
  Without the control a route that stopped touching the database at all would
  pass identically.
* **The read-only connection is read-only.** Not a convention -- SQLite refuses
  the write itself, which is why `connect_readonly` opens `mode=ro` rather than
  merely declining to call `_migrate`.
"""

import sqlite3

import pytest

from awair import db
from awair.web import create_app


@pytest.fixture
def ddl_spy(monkeypatch):
    """Count schema-bootstrap runs. `_migrate` is the whole DDL path's tail."""
    calls = []
    real = db._migrate

    def counting(conn):
        calls.append(conn)
        return real(conn)

    monkeypatch.setattr(db, "_migrate", counting)
    return calls


@pytest.fixture
def seeded(tmp_path):
    """A database the poller has already bootstrapped and written to."""
    path = tmp_path / "awair.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO readings (ts, received_at, co2, temp) VALUES (?, ?, ?, ?)",
        ("2026-09-10T01:00:00.000Z", "2026-09-10T01:00:00+00:00", 500, 22.5),
    )
    conn.commit()
    conn.close()
    return path


ROUTES = (
    "/",
    "/api/series",
    "/api/events",
    "/api/latest",
    "/api/outdoor-latest",
    "/api/outdoor-series",
)


def test_ddl_spy_sees_a_real_bootstrap(ddl_spy, tmp_path):
    """Reachability control for every absence assertion below.

    If this fails, `_migrate` is no longer the DDL path's tail and the
    zero-call assertions are vacuous rather than passing.
    """
    db.connect(tmp_path / "control.db").close()
    assert len(ddl_spy) == 1


@pytest.mark.parametrize("route", ROUTES)
def test_no_schema_ddl_per_request(route, seeded, ddl_spy):
    app = create_app(str(seeded))
    ddl_spy.clear()  # discard the one bootstrap create_app is entitled to
    client = app.test_client()
    for _ in range(3):
        assert client.get(route).status_code == 200
    assert ddl_spy == []


def test_app_startup_bootstraps_exactly_once(seeded, ddl_spy):
    create_app(str(seeded))
    assert len(ddl_spy) == 1


def test_app_startup_still_creates_a_missing_database(tmp_path, ddl_spy):
    """The self-heal today's per-request `connect()` provides is preserved.

    A `mode=ro` connection cannot create the file, so moving the bootstrap to
    startup is what keeps a fresh install -- web up before the poller has ever
    run -- serving an empty dashboard instead of 500ing.
    """
    path = tmp_path / "does-not-exist-yet.db"
    assert not path.exists()

    app = create_app(str(path))

    assert path.exists()
    assert len(ddl_spy) == 1
    client = app.test_client()
    assert client.get("/api/latest").status_code == 200
    assert len(ddl_spy) == 1


def test_connect_readonly_refuses_a_write(seeded):
    conn = db.connect_readonly(seeded)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            conn.execute("INSERT INTO readings (ts, received_at) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_connect_readonly_refuses_ddl(seeded):
    """The specific write #73 is about: the swallowed per-request ALTER."""
    conn = db.connect_readonly(seeded)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            conn.execute("ALTER TABLE readings ADD COLUMN spurious TEXT")
    finally:
        conn.close()


def test_connect_readonly_reads_an_uncheckpointed_wal_write(seeded):
    """A live writer's committed rows are visible without a checkpoint.

    The poller holds the database in WAL and writes every 30s, so a read-only
    reader that could only see checkpointed data would serve stale readings.
    """
    writer = db.connect(seeded)
    writer.execute(
        "INSERT INTO readings (ts, received_at, co2) VALUES (?, ?, ?)",
        ("2026-09-10T01:00:30.000Z", "2026-09-10T01:00:30+00:00", 999),
    )
    writer.commit()
    try:
        reader = db.connect_readonly(seeded)
        try:
            latest = reader.execute(
                "SELECT co2 FROM readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert latest[0] == 999
        finally:
            reader.close()
    finally:
        writer.close()


def test_connect_readonly_refuses_a_missing_database(tmp_path):
    """The named trade: `connect_readonly` cannot conjure the file.

    Documented as a test because it is the one behaviour change -- a database
    deleted out from under a running web process now fails the request loudly
    instead of being silently recreated empty.
    """
    with pytest.raises(sqlite3.OperationalError):
        db.connect_readonly(tmp_path / "absent.db")


def test_connect_readonly_does_not_create_the_file(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        db.connect_readonly(path)
    assert not path.exists()
