"""컨텍스트 조립.

에이전트 품질의 8할은 모델이 아니라 여기서 결정된다.
"채널에 나중에 합류해도 3주 전부터 이어받는다"를 구현하는 곳이기도 하다.

Phase 4에서 확장할 지점:
  - 채널 롤링 요약 (K개 메시지마다 백그라운드 생성)
  - 에이전트 장기 메모리 검색 (pgvector 하이브리드)
  - 태스크 보드 현황 주입
"""
from __future__ import annotations

from typing import List, Optional

from app.core.models import Agent, Channel, MemberType, Message, MessageKind
from app.store.base import Store

MAX_HISTORY = 30
MAX_TEXT = 1200


def _fmt(m: Message, *, highlight_id: Optional[str] = None) -> str:
    text = m.text if len(m.text) <= MAX_TEXT else m.text[:MAX_TEXT] + " …(생략)"
    tag = "  ← 지금 처리해야 할 메시지" if m.id == highlight_id else ""
    thread = f" (스레드 {m.thread_id[:10]})" if m.thread_id else ""
    return f"[{m.author_name}]{thread} {text}{tag}"


async def build_context_block(
    store: Store,
    agent: Agent,
    channel: Channel,
    trigger: Message,
    *,
    history_limit: int = MAX_HISTORY,
) -> str:
    """시스템 프롬프트에 들어갈 '현재 상황' 블록."""
    members = await store.channel_members(channel.id)
    roster: List[str] = []
    for cm in members:
        if cm.member_type == MemberType.AGENT:
            a = await store.get_agent(cm.member_id)
            if a is None:
                continue
            me = " (나)" if a.id == agent.id else ""
            role = a.role_prompt.splitlines()[0][:60] if a.role_prompt else ""
            roster.append(f"  - @{a.name}{me} — {role}")
        else:
            h = await store.get_human(cm.member_id)
            if h:
                roster.append(f"  - {h.name} (사람)")

    history = await store.recent_messages(channel.id, limit=history_limit)

    # 스레드에서 트리거된 경우 해당 스레드 전문을 따로 붙인다
    thread_block = ""
    thread_id = trigger.thread_id or (trigger.id if trigger.thread_id is None else None)
    if trigger.thread_id:
        tmsgs = await store.thread_messages(trigger.thread_id, limit=20)
        if tmsgs:
            thread_block = "\n## 이 스레드의 대화\n" + "\n".join(_fmt(m) for m in tmsgs)

    lines = [
        f"채널: #{channel.name}" + (f" — {channel.topic}" if channel.topic else ""),
        f"내 이름: @{agent.name}  (다른 사람이 나를 부를 때 쓰는 핸들)",
        "",
        "## 이 채널의 멤버",
        "\n".join(roster) if roster else "  (없음)",
        "",
        "## 최근 대화",
        "\n".join(_fmt(m, highlight_id=trigger.id) for m in history) or "  (없음)",
    ]
    if thread_block:
        lines.append(thread_block)

    lines += [
        "",
        "## 지금 해야 할 일",
        f"위에서 '← 지금 처리해야 할 메시지'로 표시된 [{trigger.author_name}] 의 요청을 처리한다.",
        "내 역할 범위를 벗어나는 부분은 적임자를 mention_agent 로 부른다.",
        "이미 다른 사람이 한 말을 반복하지 않는다.",
    ]
    return "\n".join(lines)
