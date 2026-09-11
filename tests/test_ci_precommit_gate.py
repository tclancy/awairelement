"""Every pre-commit hook has an enforcement point in CI (#106).

Before this, six hooks were reachable only from a clone where somebody had run
`pre-commit install` — which is no CI runner and no fresh checkout. Two of the
six turned out to be covered *incidentally* by tests that happen to parse the
relevant files, and incidental coverage is the thing this module exists to stop
being load-bearing: it holds only for as long as those tests exist, and nothing
tells you when it stops.

The assertions here are deliberately written as an **allow-list over the hook
set**, not as a ban on one spelling. A test asserting "SKIP does not mention
`check-yaml`" passes the moment someone writes `check_yaml`, adds a seventh
hook, or moves the skip to the job level. Asking instead "does every hook in
the config have an enforcement point, and is every skipped hook covered
elsewhere" cannot be satisfied by a rename.
"""

import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
JOB = "lint-and-test"

# A hook may be skipped in the pre-commit step ONLY if CI runs it another way.
# The value is the `name:` of the step that covers it, asserted to exist — so
# this table cannot drift into naming a step somebody deleted.
COVERED_ELSEWHERE = {
    "ruff": "Lint (ruff)",
    "ruff-format": "Format check (ruff)",
}


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _job():
    job = _workflow()["jobs"][JOB]
    assert job.get("steps"), f"job {JOB!r} has no steps — is this the right job?"
    return job


def _steps():
    return _job()["steps"]


def _hook_ids():
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    ids = [hook["id"] for repo in config["repos"] for hook in repo["hooks"]]
    assert len(ids) >= 6, f"only {len(ids)} hooks found — is the config parsing?"
    return ids


def _precommit_step():
    """The step that runs pre-commit, located by what it RUNS, not its name.

    A step name is prose and can be reworded; `pre-commit run` is the behaviour
    under test. Matching on the name would make this whole module green against
    a step that had been renamed and gutted.
    """
    matches = [s for s in _steps() if "pre-commit run" in s.get("run", "")]
    assert len(matches) == 1, f"expected exactly one pre-commit step, got {matches}"
    return matches[0]


def _effective_skip():
    """SKIP as pre-commit will actually see it: workflow, then job, then step.

    Read at all three levels because a SKIP set higher up silently disables a
    hook for every step under it, and a test that only inspected the step's own
    `env` would report a clean gate over a disabled one.
    """
    skip = ""
    for scope in (_workflow(), _job(), _precommit_step()):
        value = (scope.get("env") or {}).get("SKIP")
        if value:
            skip = value
    return {part.strip() for part in skip.split(",") if part.strip()}


def test_ci_runs_pre_commit_over_every_file():
    """`--all-files`, not the default. pre-commit with no file selection and no
    git hook context checks nothing at all and exits 0."""
    assert "--all-files" in _precommit_step()["run"]


def test_every_hook_has_an_enforcement_point_in_ci():
    """The assertion the issue is actually about.

    A hook is enforced if the pre-commit step runs it, or if it is skipped there
    and some other step covers it. Anything else is a hook that only fires on a
    machine where `pre-commit install` was run.
    """
    skipped = _effective_skip()
    unenforced = skipped - set(COVERED_ELSEWHERE)
    assert not unenforced, (
        f"hooks skipped in CI with no dedicated step: {sorted(unenforced)}. "
        "Either drop them from SKIP or add them to COVERED_ELSEWHERE with the "
        "step that runs them."
    )
    stale = skipped - set(_hook_ids())
    assert not stale, (
        f"SKIP names hooks that no longer exist: {sorted(stale)} — a renamed "
        "hook stops being skipped silently, which is the benign direction, but "
        "the entry is now lying about what CI does."
    )


def test_each_separately_covered_hook_really_has_its_step():
    """`COVERED_ELSEWHERE` is the excuse for skipping; make it produce evidence.

    Without this the table is a comment — someone deletes the ruff steps, the
    hooks stay in SKIP, and ruff silently stops running in CI while this module
    still reports every hook enforced.
    """
    names = {s.get("name") for s in _steps()}
    for hook_id, step_name in COVERED_ELSEWHERE.items():
        assert step_name in names, (
            f"{hook_id!r} is skipped in the pre-commit step because "
            f"{step_name!r} was meant to cover it, and that step is gone"
        )


def test_the_ruff_duplication_was_resolved_rather_than_left():
    """#106's third Done-when: not two steps nobody meant to keep.

    Resolved by keeping ruff's dedicated steps — their output names the rule and
    prints the diff, where the pre-commit hooks are the *fixing* variants and
    fail with only "files were modified by this hook" — and skipping them in the
    pre-commit step so the work is not done twice.
    """
    assert _effective_skip() == set(COVERED_ELSEWHERE), (
        "ruff should run once: in its own steps, skipped in pre-commit"
    )


def test_pre_commit_is_the_version_this_project_pins():
    """`uvx pre-commit` would resolve a different version at run time.

    Same failure #54 cost four blocked PRs to diagnose — a gate running a tool
    the lockfile does not govern — arriving through the gate that exists to
    prevent it.
    """
    assert _precommit_step()["run"].startswith("uv run --frozen pre-commit")
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    dev = pyproject["dependency-groups"]["dev"]
    assert any(spec.startswith("pre-commit") for spec in dev), (
        "pre-commit must be a pinned dev dependency for --frozen to resolve it"
    )
