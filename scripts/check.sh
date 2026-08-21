#!/usr/bin/env bash
# 전체 검증 — 사내 AI 없이 돌아간다.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.
fail=0
for t in tests/test_react.py tests/test_guards.py tests/test_router.py tests/test_registry.py \
         scripts/demo.py scripts/demo_runaway.py; do
  echo ""
  echo "═══ $t ═══"
  python3 "$t" > /tmp/genteam_check.out 2>&1
  rc=$?
  if [ $rc -ne 0 ]; then
    cat /tmp/genteam_check.out
    echo "  ✗ 실패 (exit $rc)"
    fail=1
  else
    tail -3 /tmp/genteam_check.out
  fi
done
echo ""
echo "════════════════════════════════════════"
[ $fail -eq 0 ] && echo "✓ 전체 통과" || echo "✗ 실패 있음"
exit $fail
