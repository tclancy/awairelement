"""The complexity gate must be able to go red (#57, metaframework #458).

This repo shipped a radon hook for months that could not fail: it piped radon
into `grep -E "^[A-Z]"`, and radon's unconditional `Average complexity:` summary
is the only line that pattern can match, so `&& exit 0` fired on every tree.
Silence is this gate's pass signal, which makes "did not complain" worth exactly
nothing until something proves the complaint path works.

These tests are about the *installed* gate in this repo — that the hook still
points at the script, that PKG_DIR names real Python, and that a grade-C block
makes it exit non-zero. The script's other always-green paths (uvx off PATH,
radon erroring, an empty package dir) are covered where the canonical copy
lives, in metaframework's tests for `templates/radon-gate.sh`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "radon-gate.sh"

GRADE_C_SOURCE = '''
def probe(a, b, c, d, e, f, g, h):
    """A deliberately grade-C block. radon scores this C; a working gate rejects it."""
    n = 0
    for x in (a, b, c, d, e, f, g, h):
        if x == 1:
            n += 1
        elif x == 2:
            n += 2
        elif x == 3:
            n += 3
        elif x == 4:
            n += 4
        elif x == 5:
            n += 5
        elif x == 6:
            n += 6
        elif x == 7:
            n += 7
        elif x == 8:
            n += 8
        elif x == 9:
            n += 9
        elif x == 10:
            n += 10
    return n
'''


def _hook():
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = [h for r in config["repos"] for h in r["hooks"]]
    return next(h for h in hooks if h["id"] == "radon-complexity")


def _pkg_dir():
    """The one line the template tells each project to edit."""
    for line in GATE.read_text().splitlines():
        if line.startswith("PKG_DIR="):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("scripts/radon-gate.sh has no PKG_DIR assignment")


def test_hook_entry_is_the_script_not_an_inline_pipeline():
    """A shell one-liner in `entry:` has nowhere to put the not-silent checks."""
    entry = _hook()["entry"]
    assert entry == "scripts/radon-gate.sh"
    assert GATE.is_file()


def test_hook_runs_on_every_commit_touching_python():
    hook = _hook()
    assert hook["types"] == ["python"]
    # The gate measures the whole package, not the staged subset — passing
    # filenames would let a complex function hide by not being in the commit.
    assert hook["pass_filenames"] is False


def test_pkg_dir_points_at_this_project_s_python():
    """`PKG_DIR="src"` survives a copy-paste; this repo's package is `awair`."""
    pkg = REPO / _pkg_dir()
    assert pkg.is_dir()
    assert list(pkg.glob("*.py"))


@pytest.mark.skipif(shutil.which("uvx") is None, reason="gate needs uvx to measure")
def test_gate_exits_non_zero_on_a_grade_c_block(tmp_path):
    """Prove the complaint path — the half the original hook never had.

    Runs the installed script with PKG_DIR repointed at a throwaway package, so
    the assertion is about this repo's copy of the gate rather than about
    whatever `awair/` happens to score today.
    """
    pkg = tmp_path / "probe_pkg"
    pkg.mkdir()
    (pkg / "probe.py").write_text(GRADE_C_SOURCE)
    gate = tmp_path / "radon-gate.sh"
    gate.write_text(
        GATE.read_text().replace(f'PKG_DIR="{_pkg_dir()}"', 'PKG_DIR="probe_pkg"')
    )
    gate.chmod(0o755)

    result = subprocess.run(
        [str(gate)], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "probe" in result.stdout
    assert "grade C or worse" in result.stderr


@pytest.mark.skipif(shutil.which("uvx") is None, reason="gate needs uvx to measure")
def test_gate_passes_on_this_repo_as_committed():
    """The other half: a clean tree is not blocked. Both halves or neither."""
    result = subprocess.run(
        [str(GATE)], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
