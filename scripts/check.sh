#!/usr/bin/env bash
# 전체 검증 — 사내 AI 없이 돌아간다.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.
fail=0
PY=${PY:-python3}
[ -x .venv/bin/python ] && PY=.venv/bin/python

for t in tests/test_react.py tests/test_guards.py tests/test_router.py tests/test_registry.py tests/test_tasks.py tests/test_silent_turn.py \
         scripts/demo.py scripts/demo_runaway.py; do
  echo ""
  echo "═══ $t ═══"
  $PY "$t" > /tmp/genteam_check.out 2>&1
  rc=$?
  if [ $rc -ne 0 ]; then
    cat /tmp/genteam_check.out
    echo "  ✗ 실패 (exit $rc)"
    fail=1
  else
    tail -3 /tmp/genteam_check.out
  fi
done
# Postgres 가 떠 있으면 SqlStore 검증도 함께 돌린다
if [ -n "${DATABASE_URL:-}" ] || nc -z 127.0.0.1 5432 2>/dev/null; then
  echo ""
  export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://genteam:genteam@127.0.0.1/genteam}"
  for t in tests/test_sql_store.py tests/test_tasks.py; do
    echo "═══ $t (Postgres) ═══"
    $PY "$t" > /tmp/genteam_check.out 2>&1
    if [ $? -ne 0 ]; then cat /tmp/genteam_check.out; echo "  ✗ 실패"; fail=1
    else tail -2 /tmp/genteam_check.out; fi
  done
else
  echo ""
  echo "(Postgres 미기동 — tests/test_sql_store.py 건너뜀)"
fi

echo ""
echo "════════════════════════════════════════"
[ $fail -eq 0 ] && echo "✓ 전체 통과" || echo "✗ 실패 있음"
exit $fail
