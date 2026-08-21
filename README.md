# GenTeam-like Agent Workspace

사람과 AI 에이전트가 같은 채널에서 협업하는 워크스페이스. 사내 AI를 LLM 백엔드로 사용한다.

## 핵심 설계

**채널(메시지 로그) 자체가 오케스트레이션 프로토콜이다.**
에이전트끼리 함수 호출로 대화하지 않는다. 공유된 append-only 메시지 로그에 글을 쓰고,
그 글이 디스패처를 타고 다른 에이전트를 깨운다. 이 순환이 협업의 전부다.

덕분에 —
- 전 과정이 그냥 채팅으로 보인다 (관측 공짜)
- 사람이 아무 때나 끼어들어 방향을 바꿀 수 있다
- 나중에 합류한 에이전트가 과거 전체를 읽고 바로 투입된다
- 새 에이전트 추가 = 채널에 초대. 오케스트레이션 그래프를 고칠 일이 없다

```
사람 메시지 ─┐
             ├─→ Dispatcher ─→ Agent Worker ─→ Tool 실행 ─→ 새 메시지 ─┐
에이전트 메시지┘      ↑                                                  │
                     └──────────────────────────────────────────────────┘
```

## 지금 바로 돌려보기 (사내 AI 없이)

```bash
PYTHONPATH=. python3 scripts/demo.py
```

세 에이전트(기획자 → 데이터분석가 → 개발자)가 위임 체인을 타고 협업하는 것을 볼 수 있다.
Mock provider가 **일부러 형식이 어긋난 응답**(도구명 오타, 주석 섞인 JSON, 펜스 없는 JSON)을
섞어 뱉으므로 ReAct 파서의 내성도 함께 확인된다.

```bash
PYTHONPATH=. python3 scripts/demo_runaway.py   # 폭주 제어 검증
./scripts/check.sh                             # 전체 검증
```

## LLM 백엔드 교체

애플리케이션 코드는 어느 백엔드가 쓰이는지 **전혀 모른다.** 교체는 `.env` 한 줄이다.

```bash
LLM_PROVIDER=claude_cli   # 지금: claude -p, API 키 불필요
LLM_PROVIDER=anthropic    # Anthropic API, 네이티브 tool calling
LLM_PROVIDER=corp         # 나중: 사내 AI
LLM_PROVIDER=mock         # 테스트/데모
```

| Provider | 인증 | tool calling | 특징 |
|---|---|---|---|
| `claude_cli` | 기존 Claude 구독 | ReAct 폴백 | API 키 불필요. 호출당 프로세스 스폰이라 느림 |
| `anthropic` | `ANTHROPIC_API_KEY` 또는 `ant auth login` | **네이티브** | 가장 빠르고 정확. 별도 과금 |
| `corp` | 사내 게이트웨이 | 설정에 따름 | `[EDIT 1~3]` 채운 뒤 전환 |

이게 지켜지는지 테스트가 강제한다 — `tests/test_registry.py` 가
`app/core`, `app/store`, `app/api` 안에 provider 이름이 등장하면 실패시킨다.

### 지금 바로: claude -p 로 개발 시작

```bash
npm install -g @anthropic-ai/claude-code    # Node >= 22 필요
PYTHONPATH=. python3 scripts/probe_claude_cli.py
```

`--tools ""` 로 Claude Code 자체 툴(Read/Bash/Edit)을 끄고, `--max-turns 1` 로
CLI의 자체 에이전트 루프를 막은 뒤 순수 텍스트 완성만 받아온다.
툴 호출은 우리 ReAct 레이어가 처리한다.

## 나중에: 사내 AI 연결하기

### 1. 어댑터 수정 — `app/llm/corp.py` 세 곳만

| 위치 | 내용 |
|---|---|
| `[EDIT 1]` `_headers()` | 인증 헤더 (Bearer / X-API-Key / api-key 중 택1) |
| `[EDIT 2]` `_build_request()` | 요청 바디 필드명. messages 배열이 아니라 단일 prompt면 `_flatten_to_prompt()` 사용 |
| `[EDIT 3]` `_parse_response()` | 응답 구조. OpenAI 호환/`result`·`data` 래핑/최상위 텍스트는 이미 처리됨 |

엔드포인트 경로는 `chat()` 안의 `f"{self.base_url}/chat/completions"` 를 고친다.

### 2. 환경변수

```bash
cp .env.example .env   # CORP_AI_BASE_URL / CORP_AI_API_KEY / CORP_AI_MODEL 채우기
```

### 3. 연결 확인

```bash
PYTHONPATH=. python3 scripts/probe_corp.py
```

이 스크립트가 5가지를 판정한다: 인증 · 요청 · 응답 파싱 · **네이티브 tool calling 지원 여부** ·
ReAct 형식 준수력. 4번 결과에 따라 `CORP_AI_NATIVE_TOOLS` 를 정한다.
**모르면 false로 두면 된다** — ReAct 폴백이 어떤 모델에서도 동작한다.

## 서버 기동

Python 3.13 기준 (3.11+ 필요).

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
USE_MEMORY_STORE=1 LLM_PROVIDER=claude_cli PYTHONPATH=. \
  .venv/bin/uvicorn app.api.main:app --port 8765 --reload
```

`USE_MEMORY_STORE=1` 이면 Postgres 없이 뜬다. 재시작하면 데이터는 사라진다.

### 엔드포인트

| Method | Path | 용도 |
|---|---|---|
| GET | `/healthz` | provider·모델·tool calling 지원 여부 |
| POST | `/api/humans` | 사람 생성 |
| POST | `/api/agents` | 에이전트 생성 (name = @멘션 핸들) |
| POST | `/api/channels` | 채널 생성 |
| POST | `/api/channels/{id}/members` | 사람·에이전트 초대 |
| GET | `/api/channels/{id}/members` | 로스터 조회 |
| POST | `/api/channels/{id}/messages` | 메시지 게시 → **여기서 연쇄가 시작된다** |
| GET | `/api/channels/{id}/messages` | 히스토리 조회 |
| GET | `/api/channels/{id}/stream` | SSE 실시간 구독 |

메시지 게시는 엔진에 넣고 즉시 응답한다. 에이전트 작업은 백그라운드에서 돌고
결과는 SSE 로 흘러나온다.

### 한 번에 시드하기

```bash
PYTHONPATH=. .venv/bin/python scripts/seed_api.py
```

## 구조

```
app/
├── core/                 ← 의존성 0. 어디서든 테스트 가능
│   ├── models.py         도메인 dataclass
│   ├── react.py          ★ ReAct 프롬프트 + 관대한 액션 파싱
│   ├── guards.py         ★ 폭주 제어 (깊이/토큰/실행횟수/핑퐁)
│   ├── router.py         ★ 디스패처 — 누구를 깨울 것인가
│   ├── tools.py          툴 레지스트리 = 에이전트의 능력 정의
│   ├── context.py        컨텍스트 조립
│   ├── runtime.py        에이전트 한 턴의 루프
│   ├── engine.py         순환을 굴리는 오케스트레이터
│   └── bus.py            SSE 팬아웃
├── llm/                  ← LLM 종속성이 갇혀 있는 유일한 디렉터리
│   ├── base.py           LLMProvider 인터페이스
│   ├── registry.py       ★ 교체 지점 — LLM_PROVIDER 로 백엔드 결정
│   ├── claude_cli.py     claude -p (지금)
│   ├── anthropic_api.py  Anthropic API (네이티브 tool calling)
│   ├── corp.py           사내 AI 어댑터 [EDIT 1~3] (나중)
│   └── mock.py           테스트/데모용
├── store/
│   ├── base.py           Store 인터페이스
│   ├── memory.py         인메모리 (데모/테스트)
│   └── sql.py            Postgres (프로덕션)
└── api/main.py           FastAPI
```

## 폭주 제어

멀티에이전트가 프로덕션에서 터지는 방식은 대부분 모델 품질이 아니라 이 넷이다.
4중 방어를 걸어놨다.

| 위험 | 방어 | 위치 |
|---|---|---|
| A→B→A 무한 멘션 | 깊이 한계 도달 시 `mention_agent` **도구가 목록에서 사라짐** | `guards.py` `can_mention_agents` |
| 짧은 주기 핑퐁 | 최근 실행 체인의 2-gram 반복 탐지 | `guards.py` `PingPongDetector` |
| 요금 폭발 | trace 단위 토큰·실행횟수 상한 | `guards.py` `TraceBudget` |
| 동시 응답 폭주 | 기본 reply_mode = `MENTION` (멘션돼야만 반응) | `router.py` |
| 자기 자신 트리거 | author == self 차단 + `TOOL_LOG`는 아무도 안 깨움 | `router.py` |
| 중복 실행 | `UNIQUE(agent_id, trigger_message_id)` | `sql.py` `claim_run` |
| 자기 말에 자기가 답함 | (agent, channel) 당 동시 실행 1개 락 | `engine.py` `_lock_for` |

깊이 한계에서 턴을 **실패시키지 않고** 도구만 뺏는 게 핵심이다.
에이전트는 남을 부르는 대신 사람에게 보고하고 정상 종료한다.

측정값 (`demo_runaway.py`): 가드 OFF 40회 호출 → 가드 ON 5회 호출.

## 구현 순서

- [x] **0단계** LLM 어댑터 — provider 팩토리 + claude_cli / anthropic / corp
- [x] **1단계** 툴 루프 — `post_message` / `reply_in_thread`
- [x] **2단계** 멀티 에이전트 + @멘션 라우팅 + 루프 가드
- [ ] **3단계** 태스크 보드 — `create_task` / `claim_task` / `update_task_status`
      → `tools.py` 하단에 등록만 하면 된다. `Task` 모델은 이미 있음
- [ ] **4단계** 메모리 — 채널 롤링 요약, pgvector 하이브리드 검색
      → `context.py`, `sql.py::search_messages`
- [ ] **5단계** 파일/산출물, 코드 실행 샌드박스
- [ ] **6단계** 권한, 승인 워크플로, 감사로그, 비용 대시보드
      → `AgentRun` 에 토큰이 이미 기록되고 있어 집계만 붙이면 된다

## 프로덕션 전환 지점

| 지금 | 프로덕션 |
|---|---|
| `asyncio.Queue` | Redis Streams (consumer group) — `engine.py::_queue` |
| `asyncio.Lock` | Redis 분산 락 — `engine.py::_locks` |
| 인메모리 EventBus | Redis pub/sub — `bus.py` |
| ILIKE 검색 | `to_tsvector` + pgvector 하이브리드 — `sql.py::search_messages` |
| 전체 히스토리 주입 | 롤링 요약 + 검색 — `context.py` |
