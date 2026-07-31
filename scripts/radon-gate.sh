#!/usr/bin/env bash
#
# Cyclomatic-complexity gate for pre-commit.
#
# Copied into new projects by the Project Initialization Checklist in
# metaframework's agent.md. The canonical copy lives in
# metaframework/templates/radon-gate.sh — fix bugs there, not only here.
#
# ---------------------------------------------------------------------------
# EDIT THIS ONE LINE: point it at this project's Python package directory.
# ---------------------------------------------------------------------------
PKG_DIR="awair"

set -u

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
    exit 1
}

command -v uvx >/dev/null 2>&1 ||
    fail "uvx is not on PATH, so complexity was never measured"

[ -d "$PKG_DIR" ] ||
    fail "no directory '$PKG_DIR' — point PKG_DIR at this project's package"

# One invocation gives both halves. `-n C` limits the block list to grade C and
# worse; `--total-average` appends a count and an average computed over EVERY
# block. Use `--total-average` and not `-a` — `-a` averages only the blocks `-n`
# chose to print, so `-a -n C` reports the average of your worst functions.
#
# `radon cc` exits 0 when it finds complex code (it is a report tool), so its
# exit status means only "radon itself ran or did not".
out=$(uvx radon cc "$PKG_DIR" -n C --total-average) ||
    fail "radon exited non-zero, so complexity was never measured"

# Output is the block list, a blank line, then the summary. Take the part above
# the blank line; on a clean tree that is empty.
findings=$(printf '%s\n' "$out" | sed -n '/^$/q;p')

# Report findings before asking whether anything was analysed: unparseable
# source shows up here as an `ERROR:` line and emits no summary at all, so
# checking the summary first would diagnose a syntax error as "no Python found"
# and send you off to repoint PKG_DIR.
if [ -n "$findings" ]; then
    echo "$findings"
    case "$findings" in
    *ERROR:*) fail "radon could not read the source above" ;;
    esac
    printf '%s\n' "$out" | tail -1
    fail "the blocks above are grade C or worse — refactor before committing"
fi

# No findings is the pass signal, so it has to be earned. An empty directory, a
# directory holding no Python, and PKG_DIR pointed at the wrong place all
# produce exactly the same empty output as clean code. radon names a block
# count only when it actually analysed something.
case "$out" in
*analyzed*) ;;
*) fail "no Python found under '$PKG_DIR' — point PKG_DIR at this project's package" ;;
esac
