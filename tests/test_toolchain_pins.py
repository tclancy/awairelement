"""One ruff in this repo, sourced from uv.lock (#56).

#54 cost four blocked PRs to diagnose, and its root cause was a toolchain the
repo did not control: CI re-resolved ruff at run time, so the linter CI ran was
not the linter anyone had tested with. The lockfile now settles that — but only
for the ruff invoked through `uv`. A `rev:`-pinned `ruff-pre-commit` hook is a
second, independent ruff that no lockfile governs, and on 2026-07-30 the two had
already drifted: the rev said v0.15.6 while `uv.lock` said 0.16.0.

These tests assert the *structure* that makes drift impossible (no second pin)
and then check the claim the structure is making, by asking the binary that
actually runs. A tag name is a claim about a version; the installed environment
is the version.
"""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
RUFF_HOOK_IDS = {"ruff", "ruff-format"}


def _config():
    return yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())


def _ruff_hooks():
    return [
        (repo, hook)
        for repo in _config()["repos"]
        for hook in repo["hooks"]
        if hook["id"] in RUFF_HOOK_IDS
    ]


def locked_ruff_version():
    """The ruff version `uv.lock` pins — the single source of truth."""
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    versions = [p["version"] for p in lock["package"] if p["name"] == "ruff"]
    assert len(versions) == 1, f"expected exactly one locked ruff, got {versions}"
    return versions[0]


def test_no_second_ruff_pin_exists():
    """The failure mode is structural, so the guard is structural.

    A `rev:`-pinned ruff-pre-commit block is a version pin that nothing keeps in
    step with uv.lock — Dependabot's `pre-commit` and `uv` ecosystems produce
    two PRs that cannot land atomically, so even a watched pin spends part of
    every week disagreeing with the lock.
    """
    repos = [r.get("repo", "") for r in _config()["repos"]]
    assert not [r for r in repos if "ruff-pre-commit" in r], (
        "ruff is pinned twice again — remove the ruff-pre-commit block and let "
        "the local hook read uv.lock"
    )


def test_both_ruff_hooks_run_the_projects_own_ruff():
    hooks = _ruff_hooks()
    assert {h["id"] for _, h in hooks} == RUFF_HOOK_IDS, "a ruff hook went missing"
    for _, hook in hooks:
        assert hook["language"] == "system"
        # --frozen, not bare `uv run`: a bare run re-resolves and rewrites a
        # stale uv.lock mid-commit, which is lock churn arriving through the
        # very hook that exists to prevent toolchain drift.
        assert hook["entry"].startswith("uv run --frozen ruff"), hook["entry"]


def test_ruff_hooks_still_see_every_file_type_the_vendor_hook_did():
    """`types_or` is supplied by ruff-pre-commit's own .pre-commit-hooks.yaml.

    A `local` hook inherits nothing, so dropping this line silently narrows the
    hook to whatever pre-commit's default happens to be.
    """
    for _, hook in _ruff_hooks():
        assert hook["types_or"] == ["python", "pyi", "jupyter"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to resolve ruff")
def test_the_ruff_that_actually_runs_is_the_locked_one():
    """Ask the binary, not the config. This is the assertion that has teeth."""
    result = subprocess.run(
        ["uv", "run", "--frozen", "ruff", "--version"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["ruff", locked_ruff_version()], result.stdout
