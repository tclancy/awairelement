"""Shared fixtures.

`conn` closes on teardown so leaked connections don't surface as
ResourceWarnings attributed to whatever test the GC happens to run in.
"""

import pytest

from awair import db


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()
