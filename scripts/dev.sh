#!/usr/bin/env bash
# 개발 서버 한 번에 띄우기.
#
#   ./scripts/dev.sh
#
# Postgres 확인 → venv 확인 → 필요하면 시드 → uvicorn 포그라운드 실행.
# Ctrl+C 로 종료한다.
set -u
cd "$(dirname "$0")/.."

PORT="${PORT:-8765}"
PG_BIN="/usr/local/opt/postgresql@17/bin"
DB_URL="${DATABASE_URL:-postgresql+asyncpg://genteam:genteam@127.0.0.1/genteam}"
PROVIDER="${LLM_PROVIDER:-claude_cli}"

step() { printf "\033[1;34m▸\033[0m %s\n" "$1"; }
fail() { printf "\033[1;31m✗\033[0m %s\n" "$1"; exit 1; }
good() { printf "  \033[32m✓\033[0m %s\n" "$1"; }

# ── 1. venv ────────────────────────────────────────────────────────────
step "Python 환경"
if [ ! -x .venv/bin/python ]; then
  fail "가상환경이 없습니다. 먼저 만드세요:
      /usr/local/opt/python@3.13/bin/python3.13 -m venv .venv
      .venv/bin/pip install -r requirements.txt"
fi
good "$(.venv/bin/python --version)"

# ── 2. Postgres ────────────────────────────────────────────────────────
step "Postgres"
if [ -x "$PG_BIN/pg_isready" ] && "$PG_BIN/pg_isready" -q 2>/dev/null; then
  good "기동 중"
else
  echo "  기동되어 있지 않습니다. 시작합니다…"
  brew services start postgresql@17 >/dev/null 2>&1
  for _ in $(seq 1 15); do
    "$PG_BIN/pg_isready" -q 2>/dev/null && break
    sleep 1
  done
  "$PG_BIN/pg_isready" -q 2>/dev/null \
    && good "기동 완료" \
    || fail "Postgres 를 띄우지 못했습니다.  brew services start postgresql@17"
fi

# ── 3. 포트 정리 ───────────────────────────────────────────────────────
step "포트 $PORT"
# --reload 는 감시 프로세스가 워커를 다시 띄운다. 자식만 죽이면 부활하므로
# uvicorn 프로세스 트리를 먼저 정리하고, 그다음 포트 점유자를 확인한다.
pkill -f "uvicorn app.api.main" 2>/dev/null
for _ in $(seq 1 10); do
  lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1 || break
  sleep 1
done
LEFT=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
if [ -n "$LEFT" ]; then
  echo "  아직 잡고 있습니다 (PID $LEFT) — 종료합니다"
  for pid in $LEFT; do kill "$pid" 2>/dev/null; done
  for _ in $(seq 1 5); do
    lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1 || break
    sleep 1
  done
  # reload 감시 프로세스가 SIGTERM 을 무시하고 남는 경우가 있다 → 강제 종료
  STUCK=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
  if [ -n "$STUCK" ]; then
    echo "  응답하지 않아 강제 종료합니다 (PID $STUCK)"
    for pid in $STUCK; do kill -9 "$pid" 2>/dev/null; done
    sleep 2
  fi
fi
if lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
  fail "포트 $PORT 를 비우지 못했습니다.  PORT=8766 ./scripts/dev.sh  로 다른 포트를 쓰세요."
fi
good "사용 가능"

# ── 4. claude CLI ──────────────────────────────────────────────────────
if [ "$PROVIDER" = "claude_cli" ]; then
  step "claude CLI"
  CLAUDE=$(command -v claude || ls "$HOME"/.nvm/versions/node/*/bin/claude 2>/dev/null | tail -1)
  if [ -z "${CLAUDE:-}" ]; then
    fail "claude CLI 를 찾을 수 없습니다.
      nvm use 22 && npm install -g @anthropic-ai/claude-code"
  fi
  if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    good "로그인됨"
  else
    fail "claude CLI 에 로그인되어 있지 않습니다.
      $CLAUDE auth login"
  fi
fi

# ── 5. 기동 ────────────────────────────────────────────────────────────
step "서버 기동"
echo ""
printf "  \033[1m브라우저에서 열기 →  http://127.0.0.1:%s\033[0m\n" "$PORT"
echo "  (채널이 비어 있으면 다른 터미널에서:"
echo "     PYTHONPATH=. .venv/bin/python scripts/seed_api.py )"
echo ""
echo "  Ctrl+C 로 종료"
echo ""

exec env \
  DATABASE_URL="$DB_URL" \
  LLM_PROVIDER="$PROVIDER" \
  PYTHONPATH=. \
  .venv/bin/uvicorn app.api.main:app --host 127.0.0.1 --port "$PORT" \
    ${NO_RELOAD:+} ${NO_RELOAD:---reload --reload-dir app}
