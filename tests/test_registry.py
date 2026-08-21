"""Provider 교체 검증 — 애플리케이션 코드가 백엔드를 모르는지 확인."""
from __future__ import annotations

import asyncio

from app.config import Settings
from app.core.engine import Engine
from app.core.guards import TraceBudget
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, new_id,
)
from app.core.tools import registry as tool_registry
from app.llm.registry import PROVIDERS, build_provider

SCRIPT = {
    "비서": ['```action\n{"tool":"post_message","args":{"text":"처리했습니다."}}\n```\n'
             '```action\n{"tool":"finish","args":{}}\n```'],
}


def check(label, actual, expected):
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  기대={expected} 실제={actual}"))
    return 0 if ok else 1


async def run() -> int:
    f = 0
    s = Settings()

    # 1) 모든 provider가 인스턴스화되는가 (corp 는 환경변수 필요라 제외)
    for name in ("claude_cli", "anthropic", "mock"):
        try:
            p = build_provider(name, s)
            f += check(f"{name} 생성", isinstance(p.default_model, str), True)
        except Exception as exc:
            f += check(f"{name} 생성", f"예외: {exc}", True)

    # 2) corp 는 환경변수 없으면 명확히 실패해야 한다
    try:
        build_provider("corp", s)
        f += check("corp 는 환경변수 없이 실패", False, True)
    except RuntimeError as exc:
        f += check("corp 는 환경변수 없이 실패", "CORP_AI_BASE_URL" in str(exc), True)

    # 3) 오타는 사용 가능 목록과 함께 거부
    try:
        build_provider("claude-cli", s)
        f += check("알 수 없는 이름 거부", False, True)
    except ValueError as exc:
        f += check("알 수 없는 이름 거부", all(p in str(exc) for p in PROVIDERS), True)

    # 4) ★ 핵심: 엔진/런타임/툴 어디에도 provider 이름이 등장하지 않는가
    import subprocess
    hits = subprocess.run(
        ["grep", "-rlE", "claude_cli|ClaudeCliProvider|AnthropicProvider|CorpProvider",
         "app/core", "app/store", "app/api"],
        capture_output=True, text=True,
    ).stdout.strip()
    f += check("core/store/api 에 provider 이름 없음", hits, "")
    if hits:
        print(f"       유출된 파일: {hits}")

    # 5) 엔진이 provider 를 갈아끼워도 동일하게 동작하는가
    from app.llm.mock import ScriptedProvider
    from app.store.memory import InMemoryStore

    store = InMemoryStore()
    ch = store.put_channel(Channel(id="c", workspace_id="w", name="t"))
    h = store.put_human(Human(id="h", workspace_id="w", name="석"))
    store.join(ChannelMember(ch.id, MemberType.HUMAN, h.id))
    a = store.put_agent(Agent(id="a", workspace_id="w", name="비서", role_prompt="비서다"))
    store.join(ChannelMember(ch.id, MemberType.AGENT, a.id))

    engine = Engine(store, ScriptedProvider(SCRIPT), tool_registry,
                    default_budget=TraceBudget("", max_depth=2))
    await engine.start()
    await engine.submit(Message(id=new_id("m"), channel_id=ch.id,
                                author_type=MemberType.HUMAN, author_id=h.id,
                                author_name="석", text="@비서 도와줘"))
    await engine.wait_idle()
    await engine.stop()
    said = [m.author_name for m in await store.recent_messages(ch.id)]
    f += check("교체된 provider 로 에이전트 응답", "비서" in said, True)
    return f


if __name__ == "__main__":
    import sys
    n = asyncio.get_event_loop().run_until_complete(run())
    print("-" * 60)
    print("모두 통과" if n == 0 else f"{n}건 실패")
    sys.exit(1 if n else 0)
