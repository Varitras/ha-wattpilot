#!/usr/bin/env bash
# Run one mutant the way mutmut does, but keep pytest's exit code and output.
# mutmut maps exit 1 AND exit 3 (pytest internal error) to "killed", so a
# "kill" alone does not say a test caught anything.
set -u
cd "$(dirname "$0")/.." 2>/dev/null || true
cd mutants || { echo "no mutants/ sandbox -- run 'mutmut run' first"; exit 1; }

for mutant in \
  "custom_components.wattpilot.hub.xǁWattpilotHubǁasync_shutdown__mutmut_10" \
  "custom_components.wattpilot.x_async_setup_entry__mutmut_2"; do
  echo "=============================================================="
  echo "MUTANT: $mutant"
  tests=$(cd .. && mutmut tests-for-mutant "$mutant" 2>/dev/null | tr '\n' ' ')
  echo "deckende Tests: $tests"
  MUTANT_UNDER_TEST="$mutant" PY_IGNORE_IMPORTMISMATCH=1 \
    python -m pytest $tests -q -p no:randomly 2>&1 | tail -25
  code=${PIPESTATUS[0]}
  echo "PYTEST-EXITCODE: $code   (0=survived, 1=killed durch Test, 3=INTERNER FEHLER)"
done
