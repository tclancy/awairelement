"""Every pre-commit hook has an enforcement point in CI (#106).

Before this, the hooks were reachable only from a clone where somebody had run
`pre-commit install` — which is no CI runner and no fresh checkout. Some were
covered *incidentally* by tests that happen to parse the relevant files, and
incidental coverage is what this module exists to stop being load-bearing: it
holds only for as long as those tests exist, and nothing tells you when it
stops. What #106 called six hooks enforced nowhere was measured to be two
outright — `trailing-whitespace` and `check-merge-conflict` — plus
`.github/dependabot.yml`, the one YAML file no test parsed.

The assertions are written as an **allow-list over the hook set**, not as a ban
on one spelling. A test asserting "SKIP does not mention `check-yaml`" passes
the moment someone writes `check_yaml`, adds a seventh hook, or moves the skip
to the job level. Asking instead "is every hook in the config enforced, by this
step or by a named step that demonstrably runs it" cannot be satisfied by a
rename.

Four ways a green gate can enforce nothing, each pinned below because each was
observed to survive an earlier version of this module:

* **`SKIP`** naming a hook nothing else covers.
* **`stages:`** — a hook carrying `stages: [manual]`, or a top-level
  `default_stages`, vanishes from `pre-commit run --all-files` output entirely
  and the command exits **0**. This is a second SKIP that reads nothing like one.
* **`continue-on-error` / `if:`** on the step or the job, which leave the step
  present and its verdict ignored.
* **a bare hook id** after `pre-commit run`, which runs exactly one hook.

Not covered, and deliberately: an earlier step writing `SKIP=...` to
`$GITHUB_ENV` composes into the pre-commit step's environment and is invisible
to a static read of `env:` blocks. There is no such step today; if one is ever
added, this module will not notice.
"""

import shlex
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
JOB = "lint-and-test"

# pre-commit's stage for an ordinary commit-time run. `pre-commit run
# --all-files` with no `--hook-stage` selects this one, so a hook whose
# `stages` excludes it is not run by the CI step no matter what SKIP says.
COMMIT_STAGE = "pre-commit"

# A hook may be skipped in the pre-commit step ONLY if CI runs it another way.
# Each value is a step `name:` **and a fragment of the command that step must
# run**. The name alone is not enough: rewriting `Lint (ruff)`'s `run:` to
# `echo skipping` while keeping the name left every assertion here green with
# ruff running nowhere in CI.
COVERED_ELSEWHERE = {
    "ruff": ("Lint (ruff)", "ruff check"),
    "ruff-format": ("Format check (ruff)", "ruff format --check"),
}


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _job():
    job = _workflow()["jobs"][JOB]
    assert job.get("steps"), f"job {JOB!r} has no steps — is this the right job?"
    return job


def _steps():
    return _job()["steps"]


def _config():
    return yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())


def _hooks():
    """Every (id, hook-mapping) pair in the config."""
    pairs = [
        (hook["id"], hook) for repo in _config()["repos"] for hook in repo["hooks"]
    ]
    assert pairs, "no hooks parsed — is .pre-commit-config.yaml being read?"
    return pairs


def _runs_at_commit_stage(hook) -> bool:
    """Would `pre-commit run --all-files` select this hook at all?

    A hook's own `stages` wins; otherwise the top-level `default_stages`;
    otherwise every stage. A hook excluded here is silently absent from the
    run's output and costs the command nothing — it still exits 0.
    """
    stages = hook.get("stages") or _config().get("default_stages")
    return stages is None or COMMIT_STAGE in stages


def _precommit_step():
    """The step that runs pre-commit, located by what it RUNS, not its name.

    A step name is prose and can be reworded; `pre-commit run` is the behaviour
    under test. Matching on the name would leave this module green against a
    step that had been renamed and gutted.
    """
    matches = [s for s in _steps() if "pre-commit run" in s.get("run", "")]
    assert len(matches) == 1, f"expected exactly one pre-commit step, got {matches}"
    return matches[0]


def _effective_skip():
    """SKIP as pre-commit will see it: workflow, then job, then step.

    All three, because a SKIP set higher up disables a hook for everything
    under it and a step-only read would report a clean gate over a disabled one.
    """
    skip = ""
    for scope in (_workflow(), _job(), _precommit_step()):
        value = (scope.get("env") or {}).get("SKIP")
        if value:
            skip = value
    return {part.strip() for part in skip.split(",") if part.strip()}


def _hooks_the_ci_step_runs():
    """The hooks that actually execute in the pre-commit step."""
    skipped = _effective_skip()
    return {
        hook_id
        for hook_id, hook in _hooks()
        if hook_id not in skipped and _runs_at_commit_stage(hook)
    }


# --- the allow-list: every hook is enforced somewhere ---


def test_every_hook_in_the_config_is_enforced_in_ci():
    """The assertion the issue is actually about, stated positively.

    Every hook must be run by the pre-commit step or named in
    `COVERED_ELSEWHERE`. Written over `_hooks()` rather than over `SKIP`, so a
    hook that disappears from the run for a reason SKIP knows nothing about —
    `stages: [manual]` is the live one — fails here rather than passing.
    """
    all_hooks = {hook_id for hook_id, _ in _hooks()}
    enforced = _hooks_the_ci_step_runs() | set(COVERED_ELSEWHERE)
    unenforced = all_hooks - enforced
    assert not unenforced, (
        f"hooks with no enforcement point in CI: {sorted(unenforced)}. Either "
        "let the pre-commit step run them, or add a dedicated step and name it "
        "in COVERED_ELSEWHERE."
    )


def test_no_stage_setting_quietly_removes_a_hook_from_the_run():
    """`stages:` is a second SKIP, and it is far quieter than the first.

    A hook carrying `stages: [manual]`, or a top-level `default_stages` that
    omits the commit stage, is absent from `pre-commit run --all-files` output
    altogether and the command still exits 0. Measured: adding
    `default_stages: [manual]` removes four hooks and CI stays green.
    """
    for hook_id, hook in _hooks():
        assert _runs_at_commit_stage(hook), (
            f"hook {hook_id!r} does not run at the {COMMIT_STAGE!r} stage, so "
            "the CI step skips it in silence"
        )


def test_skip_does_not_name_a_hook_that_no_longer_exists():
    """A renamed hook stops being skipped silently — the benign direction — but
    the SKIP entry is then lying about what CI does."""
    stale = _effective_skip() - {hook_id for hook_id, _ in _hooks()}
    assert not stale, f"SKIP names hooks that are not in the config: {sorted(stale)}"


# --- the step itself is real, and its verdict counts ---


def test_ci_runs_pre_commit_over_every_file():
    """`--all-files`, not the default. pre-commit with no file selection and no
    git hook context checks nothing at all and exits 0."""
    assert "--all-files" in _precommit_step()["run"]


def test_the_pre_commit_step_is_not_narrowed_to_one_hook():
    """`pre-commit run trailing-whitespace --all-files` runs exactly one hook.

    Every other assertion in this module passes against that command, because
    it still contains `pre-commit run` and `--all-files`.
    """
    tokens = shlex.split(_precommit_step()["run"])
    # The `run` that follows `pre-commit`, not the first `run` in the line --
    # the command begins `uv run --frozen pre-commit run`, so indexing on the
    # first match reads `pre-commit` itself as a named hook.
    pre_commit_at = tokens.index("pre-commit")
    after_run = tokens[tokens.index("run", pre_commit_at) + 1 :]
    positional = [t for t in after_run if not t.startswith("-")]
    assert not positional, (
        f"the pre-commit step names specific hooks {positional} — it must run "
        "all of them"
    )


def test_the_pre_commit_steps_verdict_is_not_discarded():
    """A step can be present, run, fail, and be ignored.

    All three of these left the gate green while enforcing nothing:
    `continue-on-error: true` on the step, `if: false` on the step, and
    `if: false` on the job.
    """
    step = _precommit_step()
    assert step.get("continue-on-error") is not True, (
        "the pre-commit step's failure is ignored"
    )
    assert "if" not in step, "the pre-commit step is conditional"
    assert "if" not in _job(), f"the whole {JOB!r} job is conditional"


def test_each_separately_covered_hook_really_has_a_step_that_runs_it():
    """`COVERED_ELSEWHERE` is the excuse for skipping; make it produce evidence.

    Checked on the step's `run`, not only its `name`: rewriting `Lint (ruff)`'s
    command to `echo skipping` while keeping the name is the mutation that
    survived the first version of this module, and it leaves ruff running
    nowhere in CI while every hook still reads as enforced.
    """
    steps_by_name = {s.get("name"): s for s in _steps()}
    for hook_id, (step_name, command_fragment) in COVERED_ELSEWHERE.items():
        step = steps_by_name.get(step_name)
        assert step is not None, (
            f"{hook_id!r} is skipped in the pre-commit step because "
            f"{step_name!r} was meant to cover it, and that step is gone"
        )
        assert command_fragment in step.get("run", ""), (
            f"step {step_name!r} no longer runs {command_fragment!r}, so "
            f"{hook_id!r} is skipped in CI and covered by nothing"
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
