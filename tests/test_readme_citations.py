"""Every test the README names by node id must still resolve (#114).

The README carries doctrine, not just setup, and since #114 it settles an
ownership question — *nothing on this side escalates a sustained NWS outage* —
by pointing at the test that pins it. A prose claim backed by a citation is
only as good as the citation: rename or delete the test and the README goes on
asserting a guard that no longer exists, which is worse than having made no
claim at all, because the next reader stops looking.

Written over the *pattern* rather than over today's one citation, so the next
person who reaches for this device gets the same guarantee. That generality is
itself pinned — `test_the_citation_pattern_reads_the_forms_people_write` feeds
the regex known-good and known-bad strings — because the first draft of this
file anchored on a leading backtick and a `tests/` prefix, and code review
showed three plausible citation forms slipping past it green: a node id inside
a `pytest ...` command, a parametrized id, and a hyphenated filename. A guard
that covers one spelling while advertising a general one is the same unearned
green it was written to refuse.

**Node ids containing spaces are not supported**, and fail loudly rather than
silently: the match stops at the space, and the truncated id does not collect.
That is the right direction for this file — a citation nobody can run should
be red, not invisible.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Any `<path>.py::<node>`, backticked or bare, so a node id written inside a
# `pytest ...` command line is caught too. The path is NOT anchored to `tests/`
# on purpose: a citation that omits the prefix is not a runnable address from
# the repo root, and this guard should say so rather than skip it.
CITATION = re.compile(r"\b([\w./-]*\.py::[\w:.\[\]-]+)")

COLLECT_TIMEOUT_SECONDS = 120


def _citations(text=None):
    if text is None:
        text = (REPO / "README.md").read_text()
    return CITATION.findall(text)


def test_the_citation_pattern_reads_the_forms_people_write():
    """Pin the regex, not just its output on today's README.

    Both halves matter. The positives are the forms review proved the first
    draft missed; the negatives are what stops this widening from turning an
    ordinary sentence about a module into a citation nobody can collect.
    """
    matches = _citations(
        "`tests/test_outdoor.py::test_x` and `tests/test_a.py::Klass::test_y`\n"
        "Run `pytest tests/test_b.py::test_z` to see it.\n"
        "`tests/test_c.py::test_p[case-1]` and `tests/sub-dir/test-d.py::test_q`\n"
        "bare tests/test_e.py::test_r in prose\n"
    )
    assert matches == [
        "tests/test_outdoor.py::test_x",
        "tests/test_a.py::Klass::test_y",
        "tests/test_b.py::test_z",
        "tests/test_c.py::test_p[case-1]",
        "tests/sub-dir/test-d.py::test_q",
        "tests/test_e.py::test_r",
    ]

    assert _citations("`awair/web.py` and `tests/test_web.py` are files") == []
    assert _citations("the module awair.weather_alerts, and poll_once()") == []


def test_the_readme_cites_at_least_one_test():
    """The guard below is vacuous over an empty list — so prove it is not empty.

    Fires for either of two reasons, and they need different responses: every
    citation was legitimately removed from the README (delete this file's
    premise too, then), or the pattern above stopped matching the ones that are
    still there (fix the pattern). A collection loop that runs zero times
    passes, which is the unearned green the radon gate next door also refuses.
    """
    assert _citations(), (
        "README cites no test node ids — either the last citation was removed "
        "on purpose, or CITATION has drifted from what the README writes"
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="resolving a node id needs uv")
def test_every_test_the_readme_names_can_be_collected():
    """Ask pytest to resolve each node id, rather than grepping for the name.

    A grep would pass on a test that exists but sits behind a renamed class, a
    moved file, or a collection error. `--collect-only` is the same resolution
    the real run does, and it exits 4 — not 0 — on an unresolvable id.
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
            timeout=COLLECT_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, (
            f"README cites `{node}`, which pytest cannot collect:\n"
            f"{result.stdout}\n{result.stderr}"
        )
