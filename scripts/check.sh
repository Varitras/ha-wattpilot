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

cd "$(dirname "$0")/.." || exit 1

# `mutmut run` exits 0 no matter what it found -- it only reports. The gate
# has to judge the report itself, and every outcome needs a verdict, not just
# "survived": see scripts/judge_mutants.py, which is where that policy lives
# and where it is covered by tests.
# Called by name through run_gate below, which shellcheck cannot follow.
# shellcheck disable=SC2329
mutation_gate() {
    # Discard the whole sandbox first. mutmut keeps each mutant's verdict there
    # and only retests mutants whose *source* changed -- so after adding tests
    # alone it replays the old verdicts. Measured: a rerun after 24 new tests
    # reported the identical 104 survivors until this directory was removed.
    rm -rf mutants
    mutmut run || return $?

    # --all: plain `mutmut results` prints only the mutants that were not
    # killed, so a perfect run looks exactly like a run that checked
    # nothing. Measured on a real run: 5 lines without it, 410 with.
    mutmut results --all true >mutants/results.txt || return $?

    # The accepted survivors get their verdict from a raw pytest run, not
    # from mutmut. Twice now CI has reported one of them killed while it
    # provably survived everywhere else -- and mutmut books pytest's exit 3
    # (an internal error) as a kill, swallows the output that would say
    # which, and runs the tests behind a bare TextIOBase standing in for
    # stdout and stderr. The same mutant under plain pytest, with the real
    # exit code and the output kept, has agreed with reality every time it
    # was checked (a killed neighbour fails there with a real test failure).
    # So for these few, that is the measurement the report carries; if one
    # of them ever dies here, the reason is in the log.
    accepted=$(grep -v -e '^#' -e '^$' scripts/equivalent-mutants.txt)
    for mutant in $accepted; do
        sed -i "/^ *${mutant}: /d" mutants/results.txt
        mutmut tests-for-mutant "$mutant" | grep '::' >mutants/tests-for-accepted.txt
        # One argument per test id, on purpose: the list is word-split.
        # shellcheck disable=SC2046
        if (cd mutants && MUTANT_UNDER_TEST="$mutant" python3 -m pytest -x -q --rootdir=. -p no:cacheprovider $(cat tests-for-accepted.txt)); then
            echo "    ${mutant}: survived" >>mutants/results.txt
        else
            echo "    ${mutant}: killed" >>mutants/results.txt
        fi
    done
    python3 scripts/judge_mutants.py mutants/results.txt scripts/equivalent-mutants.txt
}

run_gate "ruff format" ruff format --check custom_components tests scripts
run_gate "ruff lint" ruff check custom_components tests scripts
run_gate "mypy strict" mypy
run_gate "shellcheck" shellcheck scripts/*.sh
run_gate "pytest" python3 -m pytest

if [[ "${1:-}" == "--release" ]]; then
    run_gate "mutation run" mutation_gate
fi

if [[ $SKIPPED -eq 1 && "${ALLOW_SKIP:-0}" != "1" ]]; then
    echo "Gates were skipped and ALLOW_SKIP=1 not set — failing."
    exit 1
fi
exit $FAILED
