"""폭주 시나리오 — 가드가 실제로 무한 루프를 끊는지 확인한다.

    PYTHONPATH=. python3 scripts/demo_runaway.py

두 에이전트가 서로를 끝없이 부르도록 만들어놓고,
가드 유무에 따라 어떻게 달라지는지 비교한다.
"""
from __future__ import annotations

import asyncio
import sys
from typing import List, Optional

from app.core.engine import Engine
from app.core.guards import PingPongDetector, TraceBudget, TurnGate
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, new_id,
)
from app.core.tools import registry
from app.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolSpec, Usage
from app.store.memory import InMemoryStore

WS = "ws_loop"


class PingPongProvider(LLMProvider):
    """항상 '상대방'을 멘션한다. 가드가 없으면 영원히 멈추지 않는다."""

    supports_native_tools = False
    default_model = "pingpong"

    def __init__(self) -> None:
        self.n = 0

    async def chat(self, messages: List[ChatMessage], tools: Optional[List[ToolSpec]] = None,
                   *, model=None, temperature=0.3, max_tokens=2048) -> LLMResponse:
        self.n += 1
        system = next((m.content for m in messages if m.role == "system"), "")
        me = "핑" if "내 이름: @핑" in system else "퐁"
        other = "퐁" if me == "핑" else "핑"
        tool_names = [t.name for t in (tools or [])]

        # 깊이 한계에 닿으면 mention_agent 가 툴 목록에서 사라진다 →
        # 모델은 남은 도구로만 행동할 수밖에 없다.
        if "mention_agent" in tool_names:
            text = (
                '```action\n{"tool":"post_message","args":{"text":"%s 입니다. %s님 확인 부탁드려요."}}\n```\n'
                '```action\n{"tool":"mention_agent","args":{"agent":"%s","request":"확인 부탁드립니다"}}\n```\n'
                '```action\n{"tool":"finish","args":{}}\n```'
            ) % (me, other, other)
        else:
            text = (
                '```action\n{"tool":"post_message","args":{"text":"%s 입니다. 더 넘길 수 없어 여기서 정리해 보고드립니다."}}\n```\n'
                '```action\n{"tool":"finish","args":{}}\n```'
            ) % me
        return LLMResponse(text=text, usage=Usage(200, 60))


def seed(store: InMemoryStore):
    ch = store.put_channel(Channel(id="ch_loop", workspace_id=WS, name="루프테스트"))
    h = store.put_human(Human(id="h1", workspace_id=WS, name="석"))
    store.join(ChannelMember(ch.id, MemberType.HUMAN, h.id))
    for name in ("핑", "퐁"):
        a = store.put_agent(Agent(id=f"agt_{name}", workspace_id=WS, name=name,
                                  role_prompt=f"너는 {name}이다."))
        store.join(ChannelMember(ch.id, MemberType.AGENT, a.id))
    return ch, h


async def scenario(label: str, budget: TraceBudget, *, gate=None):
    store = InMemoryStore()
    ch, h = seed(store)
    provider = PingPongProvider()
    engine = Engine(store, provider, registry, default_budget=budget,
                    max_concurrency=2, gate=gate)
    await engine.start()

    await engine.submit(Message(
        id=new_id("msg"), channel_id=ch.id, author_type=MemberType.HUMAN,
        author_id=h.id, author_name=h.name, text="@핑 시작해주세요",
    ))

    # 폭주해도 데모가 안 끝나는 일이 없도록 상한을 건다
    try:
        await asyncio.wait_for(engine.wait_idle(), timeout=20)
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
    await engine.stop()

    s = engine.stats
    print(f"\n── {label}")
    print(f"   LLM 호출: {provider.n}회 | 에이전트 턴: {s.turns}회 | 토큰: {s.tokens:,}")
    print(f"   차단 사유: {len(s.skipped)}건")
    for x in s.skipped[:4]:
        print(f"     - {x}")
    if timed_out:
        print("   ⚠️  20초 안에 멈추지 않음 (폭주)")
    return provider.n, timed_out, s


async def main() -> int:
    print("=" * 72)
    print("폭주 제어 검증 — 두 에이전트가 서로를 끝없이 부르는 상황")
    print("=" * 72)

    # 1) 깊이 가드 + 핑퐁 탐지를 모두 끄고, 최후의 backstop(max_runs)만 남긴다
    n_off, to_off, s_off = await scenario(
        "깊이 가드 OFF + 핑퐁 탐지 OFF (max_runs 백스톱만)",
        TraceBudget("", max_depth=10**6, max_runs=40, max_tokens=10**9),
        gate=TurnGate(PingPongDetector(max_repeats=10**6)),
    )

    # 2) 권장 기본값
    n_on, to_on, s_on = await scenario(
        "가드 기본값 (max_depth=4, max_runs=20)",
        TraceBudget("", max_depth=4, max_runs=20, max_tokens=120_000),
    )

    print("\n" + "=" * 72)
    print(f"가드 OFF → LLM 호출 {n_off}회  (max_runs 백스톱에 걸려서야 멈춤)")
    print(f"가드 ON  → LLM 호출 {n_on}회")
    if n_on:
        print(f"→ {n_off / n_on:.0f}배 절감")
    ok = (not to_on) and n_on < n_off
    print("\n" + ("✓ 가드가 폭주를 차단했습니다" if ok else "✗ 가드가 동작하지 않았습니다"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
