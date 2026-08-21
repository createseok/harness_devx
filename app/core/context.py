"""컨텍스트 조립.

에이전트 품질의 8할은 모델이 아니라 여기서 결정된다.
"채널에 나중에 합류해도 3주 전부터 이어받는다"를 구현하는 곳이기도 하다.

컨텍스트는 세 층으로 쌓는다:

    [ 롤링 요약 (오래된 것 전부) ] + [ 최근 N개 원문 ] + [ 태스크 보드 ]

요약이 있으면 최근 N개만 원문으로 넣으므로 채널이 아무리 길어져도
프롬프트 크기가 유계로 유지된다. 요약 생성은 summarizer.py 가 맡는다.
"""
from __future__ import annotations

from typing import List, Optional

from app.core.models import (
    Agent, Channel, MemberType, Message, MessageKind, Task, TaskStatus,
)
from app.core.summarizer import RECENT_WINDOW
from app.store.base import Store

#: 요약이 **없을 때** 넣을 최대 개수 (요약이 생기기 전 초기 구간)
MAX_HISTORY = 30
MAX_TEXT = 1200


def _fmt(m: Message, *, highlight_id: Optional[str] = None) -> str:
    text = m.text if len(m.text) <= MAX_TEXT else m.text[:MAX_TEXT] + " …(생략)"
    tag = "  ← 지금 처리해야 할 메시지" if m.id == highlight_id else ""
    thread = f" (스레드 {m.thread_id[:10]})" if m.thread_id else ""
    return f"[{m.author_name}]{thread} {text}{tag}"


_STATUS_LABEL = {
    TaskStatus.TODO: "할 일",
    TaskStatus.IN_PROGRESS: "진행 중",
    TaskStatus.IN_REVIEW: "검토 요청",
    TaskStatus.DONE: "완료",
}


async def _task_board(store: Store, agent: Agent, channel_id: str) -> str:
    """태스크 보드 현황. 완료된 것은 빼서 컨텍스트를 아낀다."""
    tasks = [t for t in await store.list_tasks(channel_id)
             if t.status != TaskStatus.DONE]
    if not tasks:
        return ""
    lines = ["\n## 태스크 보드"]
    for t in sorted(tasks, key=lambda x: x.created_at):
        if t.assignee_id == agent.id:
            who = "**내 담당**"
        elif t.assignee_id is None:
            who = "미배정 — claim_task 로 맡을 수 있음"
        else:
            other = await store.get_agent(t.assignee_id)
            who = f"@{other.name}" if other else "다른 담당자"
        lines.append(f"  - [{t.id}] {_STATUS_LABEL[t.status]} · {who} · {t.title}")
    return "\n".join(lines)


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

    # 요약이 있으면 최근 N개만 원문으로 — 이게 컨텍스트를 유계로 만든다
    summary = await store.get_summary(channel.id)
    window = RECENT_WINDOW if summary else history_limit
    history = await store.recent_messages(channel.id, limit=window)
    task_block = await _task_board(store, agent, channel.id)

    summary_block = ""
    if summary:
        summary_block = (
            f"\n## 이전 대화 요약 ({summary.covered_count}건 압축)\n{summary.text}"
        )

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
    ]
    if summary_block:
        lines.append(summary_block)
    lines += [
        "",
        f"## 최근 대화 (마지막 {len(history)}건)",
        "\n".join(_fmt(m, highlight_id=trigger.id) for m in history) or "  (없음)",
    ]
    if task_block:
        lines.append(task_block)
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
