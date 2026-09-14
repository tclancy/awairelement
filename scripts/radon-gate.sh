#!/usr/bin/env bash
#
# Cyclomatic-complexity gate for pre-commit.
#
# Copied into new projects by the Project Initialization Checklist in
# metaframework's agent.md. The canonical copy lives in
# metaframework/templates/radon-gate.sh — fix bugs there, not only here.
#
# ---------------------------------------------------------------------------
# EDIT THIS ONE LINE: point it at this project's Python trees.
# ---------------------------------------------------------------------------
# Space-separated, and plural on purpose (#596). A package tree plus a Django
# app tree is the ordinary shape for anything in this fleet that grew a web
# front end, and the single-valued `PKG_DIR` this replaced could not express it:
# `[ -d "canvasopt webapp" ]` tests for one directory whose name contains a
# space, so the gate failed its own existence check.
#
# The tempting workaround was the dangerous one — keep it singular and name only
# the main package. The gate then goes green having silently stopped measuring
# everything else, with nothing in the output to say so: the same always-green
# class as #458, one level up. So every entry must exist (checked below) and the
# whole list is named in every failure message.
#
# Word-split into radon's argv, which means a directory whose name contains a
# space cannot be expressed here. Rename it; a quoting scheme that could express
# it would also swallow the multi-tree case this variable exists for.
PKG_DIRS="awair"

# Version used ONLY by projects that do not lock radon themselves (see the
# resolution block below). Pinned, not floating: an unpinned `uvx radon`
# re-resolves from PyPI on every cold cache, so a radon release could turn every
# repo carrying this hook red at once with no lockfile change to explain it
# (#476). Bump this the way you would bump a pre-commit `rev:` — deliberately.
RADON_PIN="radon==6.0.1"

set -u

# ---------------------------------------------------------------------------
# Which radon runs (#476)
# ---------------------------------------------------------------------------
# A gate that shells out to a linter is only as reproducible as the binary it
# resolves, and `uvx radon` resolves an ephemeral env OUTSIDE the project — so a
# project carrying `radon` in its dev group had its own pin ignored by its own
# gate, and a test suite that executes this script made a live PyPI resolution.
#
# Order of preference:
#   1. $RADON_CMD, if the caller set one (hook `entry:`, CI, a one-off run).
#   2. `uv run --frozen radon` when uv.lock declares radon — the project pinned
#      it, so the project's pin is the one that measures it. `--frozen` uses the
#      lock as written and never re-resolves.
#   3. `uvx $RADON_PIN` otherwise, so projects with no radon dependency keep
#      working, with one fewer moving part than before.
#
# This repo takes branch 2: `radon` is in the `dev` dependency group and
# `uv.lock` pins it, so the version that grades this tree is the one CI's
# `uv sync --all-groups --frozen` installs. Before #112 this copy was a
# pre-#476 template that shelled out to `uvx radon` -- unconstrained, resolved
# outside the project, and on CI's critical path since #106. Pinned by
# tests/test_radon_gate.py::test_gate_measures_with_the_radon_the_lockfile_pins.
#
# Detection reads uv.lock rather than pyproject.toml on purpose: the lock is
# what CI installs, and a `pyproject.toml` floor (`radon>=6.0.1`) is a claim
# about a version rather than a version. If uv.lock names radon and `uv run`
# cannot produce it (wrong group, broken env), this gate fails loudly rather
# than falling back to uvx — a silent fallback would put us back to measuring
# with a version nothing in the repo describes.
if [ -z "${RADON_CMD:-}" ]; then
    if [ -f uv.lock ] && grep -q '^name = "radon"$' uv.lock; then
        RADON_CMD="uv run --frozen radon"
    else
        RADON_CMD="uvx $RADON_PIN"
    fi
fi

# Dispatch sessions export VIRTUAL_ENV=<metaframework>/.venv into every shell.
# uv ignores a mismatched VIRTUAL_ENV in a project directory, but warns about it
# on stderr, and this gate's pass signal is silence. Unset it so the resolution
# above is the whole story about which environment measured this repo.
unset VIRTUAL_ENV

# Filled in by the version probe below; named in every failure message so the
# log says WHICH radon produced the verdict, not merely that radon did.
RADON_VERSION="unknown — it never ran"

# Everything below is a way of refusing to be silent for the wrong reason.
#
# This gate's pass signal is "radon printed no findings". That signal is shared
# by a tool that never ran, a directory that does not exist, and a directory
# holding no Python — none of which measured anything. The original version of
# this hook (metaframework#458) piped radon into `grep -E "^[A-Z]"`, which
# matched radon's unconditional `Average complexity:` summary line on every
# tree and so passed everything ever committed. Each check here exists because
# that class of always-green was observed, not imagined.

fail() {
    echo "radon gate: $*" >&2
    echo "radon gate: resolver '$RADON_CMD' -> radon $RADON_VERSION" >&2
    # The scope is a list now, so "which trees did this verdict cover" is no
    # longer answerable from the script's name alone (#596). A gate that
    # silently narrowed its own scope should say so in its own failure output.
    echo "radon gate: measured '$PKG_DIRS'" >&2
    exit 1
}

# $RADON_CMD is a command line, not a path: split it and ask whether its first
# word exists. uvx-off-PATH was a real always-green path (#458), and the same
# hole exists for `uv` under launchd, GUI git clients and the Dispatch sandbox.
# shellcheck disable=SC2086  # deliberate word splitting: RADON_CMD is argv
set -- $RADON_CMD
command -v "$1" >/dev/null 2>&1 ||
    fail "$1 is not on PATH, so complexity was never measured"

# Check emptiness before the loop, not instead of it: an empty PKG_DIRS runs the
# loop body zero times, so the loop alone accepts it. Measured with radon 6.0.1 —
# `radon cc -n C --total-average` with no path argument exits 2 with an argparse
# usage message, so this is a diagnosis fix rather than an always-green fix: the
# next check downstream would report "radon exited non-zero, so complexity was
# never measured" and send you looking at the resolver instead of at the scope.
[ -n "$PKG_DIRS" ] || fail "PKG_DIRS is empty, so nothing was measured"

# Every entry, not just the first. A tree that has been renamed or removed must
# stop the commit rather than quietly shrink what this gate covers.
for dir in $PKG_DIRS; do
    [ -d "$dir" ] ||
        fail "no directory '$dir' — point PKG_DIRS at this project's Python trees"
done

# Ask the binary the gate is about to invoke what it is. This is both the
# preflight (a resolver that cannot start is caught here, before its silence can
# be read as a clean tree) and the evidence (`radon 6.0.1` in the failure
# output, so a verdict that moved because the tool moved is visible in the log).
# It also warms uvx's tool env, so the measurement below cannot emit
# `Installed 4 packages` into a gate whose pass signal is silence.
# shellcheck disable=SC2086  # deliberate word splitting: RADON_CMD is argv
probe=$($RADON_CMD --version 2>&1) ||
    fail "'$RADON_CMD --version' failed, so complexity was never measured:
$probe"

# `radon --version` prints the bare version, but the resolver in front of it may
# have said something first (`Installed 5 packages in 4ms`, a uv warning). The
# version is the last line.
RADON_VERSION=$(printf '%s\n' "$probe" | tail -1)

# One invocation gives both halves. `-n C` limits the block list to grade C and
# worse; `--total-average` appends a count and an average computed over EVERY
# block. Use `--total-average` and not `-a` — `-a` averages only the blocks `-n`
# chose to print, so `-a -n C` reports the average of your worst functions.
#
# `radon cc` exits 0 when it finds complex code (it is a report tool), so its
# exit status means only "radon itself ran or did not". Keep the `|| fail`: a
# bare `out=$(...)` takes the assignment's status, not the command's, and
# discarding it was half of #458's always-green.
#
# $PKG_DIRS is unquoted so it word-splits into one argv entry per tree (#596).
# shellcheck disable=SC2086  # deliberate word splitting: RADON_CMD, PKG_DIRS are argv
out=$($RADON_CMD cc $PKG_DIRS -n C --total-average) ||
    fail "radon exited non-zero, so complexity was never measured"

# Output is the block list, a blank line, then the summary. Take the part above
# the blank line; on a clean tree that is empty.
findings=$(printf '%s\n' "$out" | sed -n '/^$/q;p')

# Report findings before asking whether anything was analysed: unparseable
# source shows up here as an `ERROR:` line and emits no summary at all, so
# checking the summary first would diagnose a syntax error as "no Python found"
# and send you off to repoint PKG_DIRS.
if [ -n "$findings" ]; then
    echo "$findings"
    case "$findings" in
    *ERROR:*) fail "radon could not read the source above" ;;
    esac
    printf '%s\n' "$out" | tail -1
    fail "the blocks above are grade C or worse — refactor before committing"
fi

# No findings is the pass signal, so it has to be earned. An empty directory, a
# directory holding no Python, and PKG_DIRS pointed at the wrong place all
# produce exactly the same empty output as clean code. radon names a block
# count only when it actually analysed something.
#
# This counts over the whole list, so a tree that exists but holds no Python
# passes as long as some other tree does. That is deliberate — `tests/` before
# any test is written is a legitimate state — and it is why the per-entry
# existence loop above is the check that keeps the scope honest.
case "$out" in
*analyzed*) ;;
*) fail "no Python found under '$PKG_DIRS' — point PKG_DIRS at this project's trees" ;;
esac
