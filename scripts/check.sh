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

# `mutmut run` exits 0 no matter what it found -- it only reports. The gate
# has to judge the report itself, and every outcome needs a verdict, not just
# "survived": see scripts/judge_mutants.py, which is where that policy lives
# and where it is covered by tests.
mutation_gate() {
    # Discard the whole sandbox first. mutmut keeps each mutant's verdict there
    # and only retests mutants whose *source* changed -- so after adding tests
    # alone it replays the old verdicts. Measured: a rerun after 24 new tests
    # reported the identical 104 survivors until this directory was removed.
    rm -rf mutants
    mutmut run || return $?

    # Re-test the accepted survivors on their own before judging. In a full
    # run mutmut reported two of them as killed on CI while they provably
    # survived locally and, run singly, survived on CI too -- and mutmut maps
    # pytest's exit 3 (an internal error) to "killed" just like exit 1, so a
    # kill out of a full run is not evidence that a test caught anything.
    # A single-mutant run has agreed across machines every time it was
    # checked, so that is the measurement the gate trusts for these few.
    accepted=$(grep -v -e '^#' -e '^$' scripts/equivalent-mutants.txt)
    if [[ -n "$accepted" ]]; then
        # shellcheck disable=SC2086 -- one argument per mutant name, on purpose
        mutmut run $accepted >/dev/null 2>&1
    fi

    # --all: plain `mutmut results` prints only the mutants that were not
    # killed, so a perfect run looks exactly like a run that checked
    # nothing. Measured on a real run: 5 lines without it, 410 with.
    mutmut results --all true >mutants/results.txt || return $?
    python3 scripts/judge_mutants.py mutants/results.txt scripts/equivalent-mutants.txt
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
