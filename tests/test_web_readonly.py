"""The web process bootstraps the schema once, then reads read-only (#73).

Three properties are under test and they fail differently, so they are
asserted separately:

* **No DDL per request.** An absence assertion, so it is paired with two
  controls rather than one. `test_ddl_spy_sees_a_real_bootstrap` proves the
  spy fires when DDL runs; `test_api_routes_open_a_read_only_connection`
  proves each route still opens a connection at all. Without the second, a
  route that stopped touching the database entirely would pass the absence
  assertion identically -- and `/` is exactly that route, so it is tested
  for what it does instead of parametrized in beside the others.
* **The read-only connection is read-only.** Not a convention -- SQLite
  refuses the write itself, which is why `connect_readonly` opens `mode=ro`
  rather than merely declining to call `_migrate`.
* **A database the bootstrap cannot open does not take the app down.** The
  availability half of the trade: before #73 an unopenable path 500ed the
  `/api/*` routes and left `/` rendering, and moving the bootstrap to startup
  must not turn that into a failed worker boot.
"""

import sqlite3

import pytest

from awair import db, web
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
def readonly_spy(monkeypatch):
    """Count read-only connections opened, i.e. requests that hit the DB."""
    calls = []
    real = db.connect_readonly

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(db, "connect_readonly", counting)
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


API_ROUTES = (
    "/api/series",
    "/api/events",
    "/api/latest",
    "/api/outdoor-latest",
    "/api/outdoor-series",
)


def test_ddl_spy_sees_a_real_bootstrap(ddl_spy, tmp_path):
    """Reachability control 1: the spy fires when DDL actually runs.

    If this fails, `_migrate` is no longer `connect()`'s tail and every
    zero-call assertion below is vacuous rather than passing.
    """
    db.connect(tmp_path / "control.db").close()
    assert len(ddl_spy) == 1


@pytest.mark.parametrize("route", API_ROUTES)
def test_api_routes_open_a_read_only_connection(route, seeded, readonly_spy):
    """Reachability control 2: the route still reaches the database.

    Without this, a route that stopped querying entirely would satisfy
    `test_no_schema_ddl_per_request` by doing nothing at all.
    """
    client = create_app(str(seeded)).test_client()
    readonly_spy.clear()
    assert client.get(route).status_code == 200
    assert len(readonly_spy) == 1


@pytest.mark.parametrize("route", API_ROUTES)
def test_no_schema_ddl_per_request(route, seeded, ddl_spy):
    app = create_app(str(seeded))
    ddl_spy.clear()  # discard the one bootstrap create_app is entitled to
    client = app.test_client()
    for _ in range(3):
        assert client.get(route).status_code == 200
    assert ddl_spy == []


def test_dashboard_renders_without_touching_the_database(seeded, readonly_spy, ddl_spy):
    """`/` is a template render with no query, which is why it is not above."""
    client = create_app(str(seeded)).test_client()
    readonly_spy.clear()
    ddl_spy.clear()
    assert client.get("/").status_code == 200
    assert readonly_spy == []
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


def test_app_startup_creates_a_missing_parent_directory(tmp_path):
    """The pollers `makedirs` before connecting; startup bootstrap must too.

    On a fresh box `~/data/awairelement/` does not exist until whichever
    process starts first creates it, and that can be the web unit.
    """
    path = tmp_path / "data" / "awairelement" / "awair.db"
    assert not path.parent.exists()

    client = create_app(str(path)).test_client()

    assert path.exists()
    assert client.get("/api/latest").status_code == 200


def test_unopenable_database_does_not_abort_app_startup(tmp_path, caplog):
    """An unopenable path 500s its requests; it does not fail the worker boot.

    `awairelement-web.service` is `Restart=always`, so a raise here is a crash
    loop rather than a degraded service -- and before #73 the same path left
    `/` rendering. The failure is logged, not concealed.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    path = blocker / "awair.db"  # parent is a regular file: makedirs must fail

    app = create_app(str(path))  # must not raise

    assert "schema bootstrap failed" in caplog.text
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/latest").status_code == 500


def test_bootstrap_swallows_a_sqlite_error(tmp_path, monkeypatch, caplog):
    """The `sqlite3.Error` arm of the same guard, driven independently.

    `makedirs` covers the `OSError` arm; without this the `sqlite3.Error` in
    the except tuple is never exercised and could be removed unnoticed.
    """

    def boom(path):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "connect", boom)
    web._bootstrap_schema(str(tmp_path / "awair.db"), _CapturingLogger(caplog))
    assert "disk I/O error" in caplog.text


class _CapturingLogger:
    """Minimal logger stand-in that routes through the `logging` package."""

    def __init__(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        self._logger = logging.getLogger("test-bootstrap")

    def warning(self, *args, **kwargs):
        self._logger.warning(*args, **kwargs)


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


def test_connect_readonly_refuses_a_missing_database_without_creating_it(tmp_path):
    """The named trade: `connect_readonly` cannot conjure the file.

    A database deleted out from under a running web process now fails the
    request loudly instead of being silently recreated empty.
    """
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        db.connect_readonly(path)
    assert not path.exists()
