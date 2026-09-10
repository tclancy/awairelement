"""`pre-commit run --all-files` stays green on a clean tree (#104).

It was red on `main`: `end-of-file-fixer` rewrote `static/uplot.min.css` and
`tests/fixtures/air_data_latest.json`, neither of which had ever carried a
trailing newline. Day to day that is invisible, because the staged-files path
only touches what you changed -- it bites whoever runs the gate repo-wide, and
it lands two unrelated files in their diff, which is how unrelated churn gets
committed.

The two files wanted opposite answers, and the split is the point:

- `static/uplot.min.css` is **third-party minified output**. Normalising it
  makes our copy differ from what upstream ships, so the next uPlot bump
  carries a spurious one-byte diff on top of the real one, forever. Excluded.
- `tests/fixtures/air_data_latest.json` is **ours**, it is a text fixture, and
  both readers pass it through `json.loads`, which does not care. Fixed once.

So there are two halves to hold, and they need different guards. The config
half is `test_*_exclu*` below, which asks the exclusion what it does to a path.
The file half is `test_every_tracked_text_file_ends_with_a_newline`, which is
the one that actually restates the ticket's headline criterion -- and note that
**CI never runs pre-commit** (`.github/workflows/ci.yml` is `uv lock --check` /
`uv sync` / ruff / pytest), so without a test in the suite, nothing between here
and a merge holds that line at all.

The exclusion is asserted as a *predicate over paths*, never by its text. A
guard comparing it to a fixed string passes on any pattern spelled that way and
says nothing about which files it covers; `^static/uplot\\.min\\.css$` and
`^static/` are both a single edit away and both wrong, in opposite directions.
So each test hands the compiled pattern a path and asks what it does.
"""

import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

# Hooks from `pre-commit-hooks` that REWRITE file content, as opposed to
# checking it. Every one of them would normalise the vendored bytes, so every
# one of them needs the exclusion -- naming only the two that #104 happened to
# involve leaves a third door open, and `mixed-line-ending` is a plausible
# addition to a repo that ships a CSS file from npm.
CONTENT_REWRITING_HOOK_IDS = frozenset(
    {
        "trailing-whitespace",
        "end-of-file-fixer",
        "mixed-line-ending",
        "fix-byte-order-marker",
        "pretty-format-json",
        "requirements-txt-fixer",
        "file-contents-sorter",
        "sort-simple-yaml",
    }
)

# The two the config carries today. Asserted separately from the set above so
# that dropping one is a visible failure rather than a quietly narrower scan.
EXPECTED_REWRITING_HOOKS = frozenset({"trailing-whitespace", "end-of-file-fixer"})


def _config():
    return yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())


def _tracked_paths():
    """Every path in the index, POSIX-relative -- what the hooks actually see.

    `git ls-files` rather than a filesystem glob: a downloaded-but-uncommitted
    asset sitting in `static/` is not something the gate will ever process, and
    failing the suite over it would be a red gate on a correct repo.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    return [chunk.decode() for chunk in out.split(b"\0") if chunk]


def _rewriting_hooks():
    return {
        hook["id"]: hook
        for repo in _config()["repos"]
        for hook in repo["hooks"]
        if hook["id"] in CONTENT_REWRITING_HOOK_IDS
    }


def _exclude_pattern():
    """The one exclusion every content-rewriting hook shares, compiled.

    Asserting they are all equal here -- rather than in a test of its own -- is
    what lets every other test speak for all of them at once.
    """
    hooks = _rewriting_hooks()
    assert set(hooks) >= EXPECTED_REWRITING_HOOKS, (
        f"expected at least {sorted(EXPECTED_REWRITING_HOOKS)}, found "
        f"{sorted(hooks)} -- if one was removed deliberately, this file's "
        "premise needs re-reading (#104)"
    )
    excludes = {hook_id: hook.get("exclude") for hook_id, hook in hooks.items()}
    unguarded = sorted(k for k, v in excludes.items() if v is None)
    assert not unguarded, (
        f"content-rewriting hook(s) with no `exclude`: {unguarded}. They will "
        "rewrite the vendored uPlot assets and turn `pre-commit run "
        "--all-files` red on a clean tree (#104)"
    )
    assert len(set(excludes.values())) == 1, (
        f"content-rewriting hooks exclude different things: {excludes}. They "
        "normalise the same bytes; the YAML anchor exists so they cannot drift"
    )
    # pre-commit matches with re.search, so compile it the way it is used.
    return re.compile(next(iter(excludes.values())))


def test_no_top_level_exclude_disables_the_hooks_wholesale():
    """`exclude:` at the top level of the config applies to *every* hook.

    It is the tempting one-line over-correction for #104, and pre-commit honours
    it in `filter_by_include_exclude(_all_filenames(args), config["files"],
    config["exclude"])` -- so it would silently stop the whitespace hooks
    touching `static/dashboard.js`, which is ours and hand-written.

    Every other test here reads *hook-level* `exclude` and cannot see this key
    at all, so without this one they would all pass over a config where both
    hooks had been disabled from above.
    """
    config = _config()
    for key in ("exclude", "files"):
        assert key not in config, (
            f"top-level `{key}:` in .pre-commit-config.yaml narrows every hook "
            "at once, including on our own source. Scope it to the hook that "
            "needs it (#104)"
        )


def test_every_vendored_minified_asset_is_excluded():
    """Counted against the index, not named -- a third vendored asset is covered.

    Naming `uplot.min.css` would pass while `uplot.iife.min.js` sat unguarded,
    which is the shape of the original bug: the failure was reported for one
    file and the tree already held two.
    """
    pattern = _exclude_pattern()
    vendored = sorted(
        path
        for path in _tracked_paths()
        if path.startswith("static/") and ".min." in Path(path).name
    )
    assert vendored, "no vendored `.min` assets tracked under static/"
    unguarded = [path for path in vendored if not pattern.search(path)]
    assert not unguarded, (
        f"vendored assets the whitespace hooks would rewrite: {unguarded}. "
        "Normalising third-party bytes makes our copy differ from what "
        "upstream ships, so every later bump carries a spurious diff (#104)"
    )


def test_the_exclusion_covers_assets_that_do_not_exist_yet():
    """Done-when #3: the guard must not be one filename -- or one shape -- wide.

    A literal `^static/uplot\\.min\\.css$` satisfies the test above and
    re-introduces the failure the moment a second library is vendored, which is
    the realistic next change rather than a hypothetical one. Each path here is
    a different way that next change plausibly arrives:

    - a second top-level bundle;
    - a `static/vendor/` subdirectory, the conventional layout once there is
      more than one library (an `[^/]*` pattern passes every other test in this
      file and re-reds the gate on this one);
    - the sourcemap and ESM build that ship *beside* a bundle, which an
      `(css|js)$` extension list silently stops covering.
    """
    pattern = _exclude_pattern()
    for future in (
        "static/chart.min.js",
        "static/chart.min.css",
        "static/vendor/chart.min.js",
        "static/vendor/uplot/uplot.min.css",
        "static/uplot.min.js.map",
        "static/uplot.min.mjs",
    ):
        assert pattern.search(future), (
            f"{future} would be rewritten -- the exclusion is pinned to the "
            "assets that happen to exist today rather than to their shape"
        )


def test_the_exclusion_does_not_swallow_our_own_files():
    """The reachability control for the two assertions above.

    Both of those are satisfied by a *wider* pattern as happily as by a correct
    one, and `^static/` is the tempting over-correction: it would also stop the
    hooks normalising `dashboard.js`, which is ours, hand-written, and exactly
    what they are for. Without this test the pair above cannot tell a working
    exclusion from a disabled hook.

    `docs/chart.min.js` pins the leading `^static/` anchor specifically -- the
    exclusion is a statement about a vendoring location, and a bare `\\.min\\.`
    would wave through a minified file committed anywhere in the tree.
    """
    pattern = _exclude_pattern()
    for ours in (
        "static/dashboard.js",
        "static/style.css",
        "templates/dashboard.html",
        "tests/fixtures/air_data_latest.json",
        "awair/poller.py",
        "docs/chart.min.js",
    ):
        assert not pattern.search(ours), (
            f"{ours} is excluded from the whitespace hooks -- the exclusion has "
            "widened past vendored minified assets and is now disabling the "
            "hooks on our own source (#104)"
        )


def test_every_tracked_text_file_ends_with_a_newline():
    """Done-when #1, restated as something the suite can hold.

    This is the half a config guard cannot reach. Pinning
    `tests/fixtures/air_data_latest.json` by name would guard exactly the file
    that has already been fixed; the realistic regression is the *next* fixture
    arriving without a trailing newline, and `pre-commit run --all-files` goes
    red on a clean tree either way.

    That matters more here than it would elsewhere, because CI does not run
    pre-commit at all -- see this module's docstring. Nothing else in the
    pipeline notices.

    Skipped, with reasons:

    - files matching the vendored exclusion, which is the whole point of it;
    - binary files, detected by a NUL byte, since `end-of-file-fixer` skips
      them too and a PNG has no business ending in `0x0a`;
    - empty files, which the hook leaves alone (`awair/__init__.py` is one).
    """
    pattern = _exclude_pattern()
    offenders = []
    for path in _tracked_paths():
        if pattern.search(path):
            continue
        data = (REPO / path).read_bytes()
        if not data or b"\x00" in data:
            continue
        if not data.endswith(b"\n"):
            offenders.append(path)
    assert not offenders, (
        f"tracked text file(s) with no trailing newline: {offenders}. "
        "`end-of-file-fixer` will rewrite them, so `pre-commit run "
        "--all-files` is red on a clean tree and their diff lands in the next "
        "unrelated branch (#104)"
    )


def test_the_newline_scan_actually_reaches_files():
    """Reachability control for the absence assertion above.

    That test passes by finding nothing, so it also passes when it inspects
    nothing -- a `git ls-files` that returned empty, a `cwd` pointing somewhere
    else, or an exclusion widened to `^` would all read as success. This pins
    the scan to a non-trivial number of files and to two it must be looking at.
    """
    pattern = _exclude_pattern()
    scanned = [path for path in _tracked_paths() if not pattern.search(path)]
    assert len(scanned) > 20, f"only {len(scanned)} files scanned -- see #104"
    assert "tests/fixtures/air_data_latest.json" in scanned
    assert "static/dashboard.js" in scanned
