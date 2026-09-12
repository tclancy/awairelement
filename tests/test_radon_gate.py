"""The complexity gate must be able to go red (#57, metaframework #458).

This repo shipped a radon hook for months that could not fail: it piped radon
into `grep -E "^[A-Z]"`, and radon's unconditional `Average complexity:` summary
is the only line that pattern can match, so `&& exit 0` fired on every tree.
Silence is this gate's pass signal, which makes "did not complain" worth exactly
nothing until something proves the complaint path works.

These tests are about the *installed* gate in this repo — that the hook still
points at the script, that the scope names real Python, that a grade-C block
makes it exit non-zero, and (since #112) that the radon doing the grading is
the one `uv.lock` pins rather than one `uvx` resolved from PyPI. The script's
other always-green paths (the resolver off PATH, radon erroring, an empty
package dir) are covered where the canonical copy lives, in metaframework's
tests for `templates/radon-gate.sh`.
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


def _pkg_dir_assignment():
    """The whole scope line the template tells each project to edit.

    Matched by prefix rather than by exact name so this file reads both the
    singular `PKG_DIR` the gate shipped with and the plural `PKG_DIRS` the
    canonical copy moved to (metaframework #596) -- a test that can only see
    one of them goes green by not finding the other.
    """
    for line in GATE.read_text().splitlines():
        if line.startswith(("PKG_DIR=", "PKG_DIRS=")):
            return line
    raise AssertionError("scripts/radon-gate.sh has no PKG_DIR/PKG_DIRS assignment")


def _pkg_dirs():
    """Every tree the installed gate measures, as a list."""
    return _pkg_dir_assignment().split("=", 1)[1].strip().strip('"').split()


def _locked_radon_version():
    """The radon `uv.lock` pins — the version #112 says must do the grading."""
    lines = (REPO / "uv.lock").read_text().splitlines()
    stanza = lines.index('name = "radon"')
    key, _, value = lines[stanza + 1].partition("=")
    assert key.strip() == "version", (
        f"uv.lock radon stanza shape changed: {lines[stanza + 1]!r}"
    )
    return value.strip().strip('"')


def _gate_over(tmp_path, probe_dir):
    """A copy of the installed gate, repointed at `probe_dir` and executable.

    A copy rather than an env override on purpose: the thing under test is the
    script this repo commits, and the canonical copy deliberately refuses a
    plain environment override of its own scope (metaframework #596).
    """
    gate = tmp_path / "radon-gate.sh"
    scope = _pkg_dir_assignment()
    gate.write_text(
        GATE.read_text().replace(scope, f'{scope.split("=")[0]}="{probe_dir}"')
    )
    gate.chmod(0o755)
    return gate


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


def test_pkg_dirs_point_at_this_project_s_python():
    """The template default survives a copy-paste; this repo's package is `awair`.

    Every entry, not just the first: the scope is a space-separated list, and a
    tree that has been renamed or removed must be a loud failure rather than a
    gate that silently measures less than it says it does.
    """
    dirs = _pkg_dirs()
    assert dirs, "the gate must name at least one tree"
    assert "src" not in dirs, "the template's placeholder was never repointed"
    for name in dirs:
        pkg = REPO / name
        assert pkg.is_dir(), name
        assert list(pkg.glob("*.py")), name


@pytest.mark.skipif(shutil.which("uvx") is None, reason="gate needs uvx to measure")
def test_gate_exits_non_zero_on_a_grade_c_block(tmp_path):
    """Prove the complaint path — the half the original hook never had.

    Runs the installed script with its scope repointed at a throwaway package,
    so the assertion is about this repo's copy of the gate rather than about
    whatever `awair/` happens to score today.
    """
    pkg = tmp_path / "probe_pkg"
    pkg.mkdir()
    (pkg / "probe.py").write_text(GRADE_C_SOURCE)

    result = subprocess.run(
        [str(_gate_over(tmp_path, "probe_pkg"))],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
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


def test_gate_measures_with_the_radon_the_lockfile_pins(tmp_path):
    """#112: the gate must not resolve radon from outside the project.

    `uvx radon` installs an unconstrained requirement into an ephemeral
    environment, so the version grading this repo is one `uv.lock` does not
    govern -- the same shape as #54. The gate names its own resolver and
    version on every failure, so the way to read which radon ran is to make it
    fail on purpose. Run from the repo root, because that is where `uv.lock`
    is and the resolution is a question about the project it runs in.
    """
    probe = tmp_path / "probe_pkg"
    probe.mkdir()
    (probe / "probe.py").write_text(GRADE_C_SOURCE)

    result = subprocess.run(
        [str(_gate_over(tmp_path, probe))],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "uv run --frozen radon" in result.stderr, result.stderr
    assert f"radon {_locked_radon_version()}" in result.stderr, result.stderr
    assert "uvx" not in result.stderr, result.stderr
