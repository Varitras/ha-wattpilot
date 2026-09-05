# Test suite

Run: `bash scripts/check.sh` (all gates) — inside WSL2 for the full suite.

## Guard index

One line per guard; deleting a guard must show up here in the diff.

| Guard | Holds |
|---|---|
| test_domain_literal_only_in_const | domain literal only in const.py among Python files (dev-copy safety) |
| test_domain_literal_confined_to_known_carriers | domain literal never gains a fourth carrier beyond const.py/manifest.json/services.yaml |
| test_manifest_is_consistent | manifest domain/iot_class/requirements/config_flow frozen |
| test_device_fixture_is_anonymized | committed device snapshot carries no owner identifiers |
| test_diagnostics_covers_every_drop_key | redaction.sanitize_snapshot drops every credential/token key from diagnostics output |
| test_full_parity | every fork unique_id preserved; only whs/whb/whg/who added |
| test_uid_suffixes_unique_per_platform | no duplicate unique_ids within a platform |
| test_wattpilot_api_only_imported_by_hub | lib swappable: one import site (hub.py) |
| test_platform_modules_do_not_import_each_other | platforms independent |
| test_file_size_budget | size ceiling frozen; shrunk files lose their budget |
| test_cluster_size_budget | a unit split across files is measured as one, so splitting cannot buy room |
| test_complexity_ratchet | exact cc ratchet via radon; regenerate via scripts/complexity_snapshot.py |
| mutation run (check.sh --release) | tests are real: mutants in hub/sensor/init must die |
| test_ci_runs_the_same_gates_as_check_sh | CI executes scripts/check.sh + hassfest + HACS validate |

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
