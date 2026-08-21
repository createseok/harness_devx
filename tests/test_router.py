"""디스패처 라우팅 검증."""
from __future__ import annotations

import asyncio

from app.core.guards import TraceBudget
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, MessageKind, ReplyMode,
)
from app.core.router import Router, extract_mentions
from app.store.memory import InMemoryStore

WS = "w1"


def build(agent_specs, human_count=1, is_dm=False):
    s = InMemoryStore()
    ch = s.put_channel(Channel(id="c1", workspace_id=WS, name="general", is_dm=is_dm))
    for i in range(human_count):
        h = s.put_human(Human(id=f"h{i}", workspace_id=WS, name=f"사람{i}"))
        s.join(ChannelMember("c1", MemberType.HUMAN, h.id))
    for name, mode in agent_specs:
        a = s.put_agent(Agent(id=f"agt_{name}", workspace_id=WS, name=name,
                              role_prompt="...", reply_mode=mode))
        s.join(ChannelMember("c1", MemberType.AGENT, a.id))
    return s


def msg(text, author_type=MemberType.HUMAN, author_id="h0", kind=MessageKind.CHAT):
    return Message(id="m1", channel_id="c1", author_type=author_type, author_id=author_id,
                   author_name="테스터", text=text, kind=kind, trace_id="t1")


def check(label, actual, expected):
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  기대={expected} 실제={actual}"))
    return 0 if ok else 1


async def run() -> int:
    f = 0
    b = TraceBudget("t1")

    f += check("멘션 파싱(한글/영문/문장부호)",
               extract_mentions("@분석가 님과 @dev_lee, 확인 부탁드립니다 @분석가"),
               ["분석가", "dev_lee"])

    # 기본 MENTION 모드: 멘션된 1명만
    s = build([("분석가", ReplyMode.MENTION), ("개발자", ReplyMode.MENTION)])
    r = await Router(s).route(msg("@분석가 결제 실패율 좀 봐주세요"), b)
    f += check("멘션된 에이전트만 깨어남", [t.agent.name for t in r.targets], ["분석가"])

    # 멘션 없으면 아무도 안 깨어남 = 폭주 방지 1차 방어선
    r = await Router(s).route(msg("오늘 날씨 좋네요"), b)
    f += check("멘션 없으면 침묵", len(r.targets), 0)

    # reply_mode=ALL 은 멘션 없어도 반응
    s2 = build([("분석가", ReplyMode.MENTION), ("서기", ReplyMode.ALL)])
    r = await Router(s2).route(msg("회의 시작합니다"), b)
    f += check("ALL 모드는 멘션 없이 반응", [t.agent.name for t in r.targets], ["서기"])

    # 멘션 + ALL 동시 → 멘션된 쪽이 먼저
    r = await Router(s2).route(msg("@분석가 봐주세요"), b)
    f += check("멘션이 우선순위", [t.agent.name for t in r.targets], ["분석가", "서기"])

    # DM: 사람1 + 에이전트1 → 멘션 불필요
    s3 = build([("분석가", ReplyMode.MENTION)], human_count=1)
    r = await Router(s3).route(msg("이거 분석해줘"), b)
    f += check("1:1 채널은 멘션 없이 반응", [t.agent.name for t in r.targets], ["분석가"])

    # 자기 자신 멘션 → 안 깨어남
    s4 = build([("분석가", ReplyMode.MENTION), ("개발자", ReplyMode.MENTION)])
    r = await Router(s4).route(
        msg("@분석가 제가 정리했습니다", MemberType.AGENT, "agt_분석가"), b)
    f += check("자기 자신 멘션은 무시", len(r.targets), 0)

    # 툴 로그는 절대 트리거하지 않음 (이게 없으면 내부 기록이 폭주를 만든다)
    s5 = build([("서기", ReplyMode.ALL)])
    r = await Router(s5).route(msg("@서기 내부기록", kind=MessageKind.TOOL_LOG), b)
    f += check("TOOL_LOG는 아무도 안 깨움", len(r.targets), 0)

    # 예산 소진 시 skip 사유가 남는가
    dead = TraceBudget("t9", max_runs=1)
    dead.runs_spent = 5
    s6 = build([("분석가", ReplyMode.MENTION)], human_count=2)
    r = await Router(s6).route(msg("@분석가 도와줘"), dead)
    f += check("예산 소진 시 차단", len(r.targets), 0)
    f += check("차단 사유 기록됨", len(r.skipped) == 1 and "실행 횟수" in r.skipped[0][1], True)
    print(f"       → skipped: {r.skipped}")
    return f


if __name__ == "__main__":
    import sys
    n = asyncio.get_event_loop().run_until_complete(run())
    print("-" * 60)
    print("모두 통과" if n == 0 else f"{n}건 실패")
    sys.exit(1 if n else 0)
