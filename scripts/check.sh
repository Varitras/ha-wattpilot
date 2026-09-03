#!/usr/bin/env bash
# Definition of Done — every gate in one command. A silent skip is a lie:
# skipped gates print loudly and fail unless ALLOW_SKIP=1 is set on purpose.
set -u
FAILED=0
SKIPPED=0

run_gate() {
    local name="$1"; shift
    if ! command -v "$1" >/dev/null 2>&1 && [[ "$1" != python* ]]; then
        echo "SKIPPED (loud): ${name} — '$1' not installed"
        SKIPPED=1
        return
    fi
    echo "==> ${name}"
    if ! "$@"; then
        echo "FAILED: ${name}"
        FAILED=1
    fi
}

cd "$(dirname "$0")/.."

# `mutmut run` exits 0 no matter how many mutants survive -- it only reports.
# The gate has to judge the result itself, or a release whose tests kill
# nothing would sail through silently. Judged by NAME, not by count: a count
# lets a newly introduced survivor hide behind a fixed one.
mutation_gate() {
    # Discard the whole sandbox first. mutmut keeps each mutant's verdict there
    # and only retests mutants whose *source* changed -- so after adding tests
    # alone it replays the old verdicts. Measured: a rerun after 24 new tests
    # reported the identical 104 survivors until this directory was removed.
    rm -rf mutants
    mutmut run || return $?
    mutmut results | sed -n 's/^ *\(.*\): survived$/\1/p' | sort >mutants/survived
    grep -v -e '^#' -e '^$' scripts/equivalent-mutants.txt | sort >mutants/allowed
    if ! diff -u mutants/allowed mutants/survived; then
        echo "Mutation survivors differ from scripts/equivalent-mutants.txt."
        echo "  '+' lines survived but are not justified there — write a test."
        echo "  '-' lines no longer survive — remove them from that file."
        return 1
    fi
}

run_gate "ruff format" ruff format --check custom_components tests scripts
run_gate "ruff lint" ruff check custom_components tests scripts
run_gate "mypy strict" mypy
run_gate "pytest" python3 -m pytest

if [[ "${1:-}" == "--release" ]]; then
    run_gate "mutation run" mutation_gate
fi

if [[ $SKIPPED -eq 1 && "${ALLOW_SKIP:-0}" != "1" ]]; then
    echo "Gates were skipped and ALLOW_SKIP=1 not set — failing."
    exit 1
fi
exit $FAILED
