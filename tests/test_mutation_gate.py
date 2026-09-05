"""The release gate's own judgement, exercised instead of assumed.

The policy used to live in a sed expression inside check.sh that pulled out
the survivors and nothing else. A mutant reported as "no tests" therefore
disappeared from the comparison, the accepted survivors still matched, and
the gate passed while a module inside it was measurably uncovered
(audit A11-10). A gate nobody can run against a known input is a gate nobody
has checked.
"""

from __future__ import annotations

from scripts.judge_mutants import judge

ACCEPTED = """
# Justified elsewhere; the file carries the reasoning.
x_descriptions_py__mutmut_7
"""


def _report(*lines: str) -> str:
    return "Mutation results\n" + "\n".join(f"  {line}" for line in lines) + "\n"


def test_a_clean_run_passes() -> None:
    report = _report(
        "x_hub_py__mutmut_1: killed",
        "x_descriptions_py__mutmut_7: survived",
    )
    assert judge(report, ACCEPTED) == []


def test_an_unjustified_survivor_fails() -> None:
    report = _report(
        "x_hub_py__mutmut_1: survived",
        "x_descriptions_py__mutmut_7: survived",
    )
    complaints = judge(report, ACCEPTED)
    assert any("x_hub_py__mutmut_1" in c and "not justified" in c for c in complaints)


def test_a_stale_justification_fails() -> None:
    report = _report("x_hub_py__mutmut_1: killed")
    complaints = judge(report, ACCEPTED)
    assert any("no longer survives" in c for c in complaints)


def test_every_unjudged_outcome_fails_the_gate() -> None:
    """The finding itself: these are holes in the measurement, and mutmut's
    own run does not fail on any of them."""
    for outcome in ("no tests", "not checked", "suspicious", "timeout"):
        report = _report(
            "x_descriptions_py__mutmut_7: survived",
            f"x_hub_py__mutmut_9: {outcome}",
        )
        complaints = judge(report, ACCEPTED)
        assert any("x_hub_py__mutmut_9" in c for c in complaints), outcome


def test_an_outcome_the_policy_does_not_know_fails() -> None:
    """mutmut may grow a verdict we have not decided about. Fail closed."""
    report = _report(
        "x_descriptions_py__mutmut_7: survived",
        "x_hub_py__mutmut_9: bewildered",
    )
    assert any("unknown mutmut outcome" in c for c in judge(report, ACCEPTED))


def test_a_run_that_killed_everything_passes() -> None:
    """The report is read with `--all`, so every killed mutant is listed.
    Without it the report holds only the mutants that were not killed, and a
    perfect run is indistinguishable from a run that checked nothing -- the
    emptiness check below would then fire on the best possible outcome.
    Measured on a real run: 5 lines without the flag, 410 with it.
    """
    report = _report(*(f"x_hub_py__mutmut_{i}: killed" for i in range(3)))
    assert judge(report, "") == []


def test_an_empty_report_is_not_a_pass() -> None:
    """A run that checked nothing reported nothing, and the old gate read
    that as zero survivors.

    Nothing accepted either: with an accepted survivor in play this test
    passes on the stale-justification complaint instead, which is how it
    stayed green the first time the emptiness check was sabotaged.
    """
    assert judge("Mutation results\n", "") != []
