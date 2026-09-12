"""Every test the README names by node id must still resolve (#114).

The README carries doctrine, not just setup, and since #114 it settles an
ownership question — *nothing on this side escalates a sustained NWS outage* —
by pointing at the test that pins it. A prose claim backed by a citation is
only as good as the citation: rename or delete the test and the README goes on
asserting a guard that no longer exists, which is worse than having made no
claim at all, because the next reader stops looking.

Generic over the README rather than hard-coded to today's one citation, so the
next person who reaches for this device gets the same guarantee for free.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# `tests/test_foo.py::test_bar`, the only form the README uses. Deliberately
# not matching a bare `test_bar`: prose mentions a test's name all the time
# without meaning it as a runnable address, and a pattern that swept those up
# would make this file fail on an ordinary sentence.
CITATION = re.compile(r"`(tests/[\w/]+\.py::[\w:]+)`")


def _citations():
    return CITATION.findall((REPO / "README.md").read_text())


def test_the_readme_cites_at_least_one_test():
    """The guard below is vacuous over an empty list — so prove it is not empty.

    Without this, deleting every citation from the README (or breaking the
    regex) turns the collection check into a loop that runs zero times and
    passes, which is the same unearned green the radon gate exists to refuse.
    """
    assert _citations(), "README cites no test node ids — has the pattern drifted?"


def test_every_test_the_readme_names_can_be_collected():
    """Ask pytest to resolve each node id, rather than grepping for the name.

    A grep would pass on a test that exists but sits behind a renamed class, a
    moved file, or a collection error. `--collect-only` is the same resolution
    the real run does.
    """
    for node in _citations():
        result = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "--collect-only",
                "-q",
                "--no-cov",
                node,
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"README cites `{node}`, which pytest cannot collect:\n"
            f"{result.stdout}\n{result.stderr}"
        )
