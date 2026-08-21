"""발화 없이 끝나는 턴 방지.

조회 도구만 쓰고 finish 하는 경우가 실제로 관찰됐다. 사람 입장에서는
무응답과 구분되지 않으므로, finish 요약이라도 채널에 남겨야 한다.
"""
from __future__ import annotations
import asyncio, sys
from app.core.engine import Engine
from app.core.guards import TraceBudget
from app.core.models import (Agent, Channel, ChannelMember, Human, MemberType, Message, new_id)
from app.core.tools import registry
from app.llm.mock import ScriptedProvider
from app.store.memory import InMemoryStore

# 조회 도구만 쓰고 발화 없이 finish 하는 에이전트
SCRIPT = {"비서": ['```action\n{"tool":"list_members","args":{}}\n```\n'
                   '```action\n{"tool":"finish","args":{"summary":"멤버만 확인하고 끝냄"}}\n```']}

async def main():
    s = InMemoryStore()
    ch = s.put_channel(Channel(id="c", workspace_id="w", name="t"))
    h = s.put_human(Human(id="h", workspace_id="w", name="석"))
    s.join(ChannelMember(ch.id, MemberType.HUMAN, h.id))
    a = s.put_agent(Agent(id="a", workspace_id="w", name="비서", role_prompt="비서다"))
    s.join(ChannelMember(ch.id, MemberType.AGENT, a.id))
    e = Engine(s, ScriptedProvider(SCRIPT), registry, default_budget=TraceBudget("", max_depth=2))
    await e.start()
    await e.submit(Message(id=new_id("m"), channel_id=ch.id, author_type=MemberType.HUMAN,
                           author_id=h.id, author_name="석", text="@비서 도와줘"))
    await e.wait_idle(); await e.stop()
    msgs = await s.recent_messages(ch.id)
    said = [m.text for m in msgs if m.author_name == "비서"]
    ok = len(said) == 1 and "멤버만 확인하고 끝냄" in said[0]
    print(f"[{'PASS' if ok else 'FAIL'}] 발화 없이 finish 해도 침묵하지 않음 → {said}")
    return 0 if ok else 1

sys.exit(asyncio.run(main()))

