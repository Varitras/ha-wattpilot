# Test suite

Run: `bash scripts/check.sh` (all gates) — inside WSL2 for the full suite.

## Guard index

The index lives in `tests/test_guards.py::GUARD_INDEX`, one line per guard
saying what it holds, and `test_every_guard_is_listed_in_the_index` fails
when a guard is added or deleted without touching it. Deleting a guard can
therefore not be a green diff.

It used to be repeated as a table here. That copy drifted: by 2026-09-06 it
named two guards that no longer existed under those names and omitted eight
that did -- while the enforced index was correct the whole time. A second
hand-maintained list of the same rule is the bug of tomorrow, so this file
points at the enforced one instead of restating it.

The one guard outside that index, because it is not a test (entity parity
with the fork is checked in each platform's own test file):

| Guard | Holds |
|---|---|
| mutation run (check.sh --release) | tests are real: every mutant in hub/sensor/init must die, except the equivalent ones in scripts/equivalent-mutants.txt, which must survive exactly |

## Recorded frame sequences

`fixtures/replay/` holds frame sequences recorded read-only from a real
charger, sanitized with the integration's own redaction tables before they
were written, and reviewed before they were committed. `test_replay.py`
feeds them through the client in their recorded order: they test the client
against what the charger actually sends, where the other tests feed frames
written by hand.

The file name carries the firmware version. A new firmware gets new
recordings next to these, never edits to them, and
`test_replay_fixtures_are_anonymized` checks every recording the way the
device snapshot is checked.

## The pre-push hook is not in this repository

`.git/hooks/pre-push` runs three fail-closed gates before anything leaves the
machine: `scripts/check.sh`, a blocklist over the commit messages and added
lines of the push range, and `gitleaks` over the whole history.

It is deliberately **not tracked**. Every gate needs an absolute path into
WSL — the suite only runs there, and the `gitleaks` on the Windows PATH is an
older build without the `git` subcommand — and the blocklist itself lives
outside the repository, because several of its entries are exactly the words
it keeps out. **A fresh clone therefore has no hook.** The template it was
adapted from is in the erosionsschutz kit.

Why a blocklist next to `gitleaks`: on 2026-09-05 `gitleaks` reported zero
findings over the full history while a test fixture carried the repository
owner's first name as a card holder and device name. `gitleaks` knows the
shapes of secrets, not words that identify a person. Each of the three gates
was broken on purpose once and watched to refuse before any of them was
believed.
