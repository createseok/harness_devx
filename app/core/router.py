"""디스패처: 새 메시지가 올라왔을 때 "누구를 깨울 것인가"를 판정한다.

GenTeam의 규칙을 그대로 옮겼다:
  - 기본은 @멘션 된 에이전트만 반응 (폭주 방지 1차 방어선)
  - reply_mode=ALL 인 에이전트는 채널의 모든 메시지에 반응
  - DM(사람 1 + 에이전트 1)은 멘션 없이도 항상 반응
  - 자기 자신의 메시지로는 절대 깨어나지 않음
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.core.guards import TraceBudget, TurnGate
from app.core.models import Agent, Channel, MemberType, Message, MessageKind, ReplyMode
from app.store.base import Store

# @핸들 — 한글/영문/숫자/밑줄/하이픈 허용, 뒤에 문장부호가 붙어도 잡힘
MENTION_RE = re.compile(r"@([A-Za-z0-9_\-가-힣]{1,40})")


def extract_mentions(text: str) -> List[str]:
    """중복 제거 + 등장 순서 유지."""
    seen = []
    for m in MENTION_RE.findall(text or ""):
        if m not in seen:
            seen.append(m)
    return seen


@dataclass
class WakeTarget:
    agent: Agent
    reason: str          # "mention" | "reply_all" | "dm"
    budget: TraceBudget


@dataclass
class RouteResult:
    targets: List[WakeTarget]
    skipped: List[Tuple[str, str]]   # (agent_name, 사유)


class Router:
    def __init__(self, store: Store, gate: Optional[TurnGate] = None) -> None:
        self.store = store
        self.gate = gate or TurnGate()

    async def route(self, message: Message, budget: TraceBudget) -> RouteResult:
        targets: List[WakeTarget] = []
        skipped: List[Tuple[str, str]] = []

        # 툴 로그/시스템 메시지는 절대 에이전트를 깨우지 않는다.
        # (이걸 빠뜨리면 에이전트의 내부 기록이 다른 에이전트를 깨워 폭주한다)
        if message.kind != MessageKind.CHAT:
            return RouteResult([], [])

        channel = await self.store.get_channel(message.channel_id)
        if channel is None:
            return RouteResult([], [])

        members = await self.store.channel_members(message.channel_id)
        agent_members = [m for m in members if m.member_type == MemberType.AGENT]
        human_count = sum(1 for m in members if m.member_type == MemberType.HUMAN)

        mentions = {m.lower() for m in extract_mentions(message.text)}
        # DM 판정: 사람 1명 + 에이전트 1명 → 멘션 불필요
        is_dm_like = channel.is_dm or (human_count == 1 and len(agent_members) == 1)

        for cm in agent_members:
            agent = await self.store.get_agent(cm.member_id)
            if agent is None:
                continue

            effective_mode = cm.reply_mode or agent.reply_mode

            if agent.name.lower() in mentions:
                reason = "mention"
            elif is_dm_like:
                reason = "dm"
            elif effective_mode == ReplyMode.ALL:
                reason = "reply_all"
            else:
                continue  # 멘션 안 됐고 ALL도 아니면 조용히 있는다

            decision = self.gate.check(
                agent_id=agent.id,
                author_type=message.author_type.value,
                author_id=message.author_id,
                budget=budget,
                agent_enabled=agent.enabled,
            )
            if not decision.allowed:
                skipped.append((agent.name, decision.reason))
                continue

            targets.append(WakeTarget(agent=agent, reason=reason, budget=budget))

        # 멘션된 에이전트를 먼저 깨운다 (사용자가 지목한 쪽이 우선)
        targets.sort(key=lambda t: 0 if t.reason == "mention" else 1)
        return RouteResult(targets, skipped)
