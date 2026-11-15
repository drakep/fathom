#!/usr/bin/env bash
# Runs the full Fathom test suite with the pinned compiler.
#   scripts/test.sh            Fe unit/property/contract tests
#   scripts/test.sh --harness  also the differential accuracy harness
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FE="${FE:-fe}"
WANT="$(cat "$ROOT/FE_VERSION")"
GOT="$("$FE" --version | awk '{print $2}')"
if [ "$GOT" != "$WANT" ]; then
  echo "Fe $WANT required, found $GOT. Run scripts/install-fe.sh" >&2
  exit 1
fi
"$FE" test "$ROOT"
if [ "${1:-}" = "--harness" ]; then
  FE="$FE" python3 "$ROOT/harness/run.py" --samples "${SAMPLES:-1000}"
fi
