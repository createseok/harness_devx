"""툴 레지스트리.

에이전트의 능력은 프롬프트가 아니라 **여기 등록된 툴 목록**이 정의한다.
새 기능을 에이전트에게 주고 싶으면 여기에 툴을 하나 추가하면 된다.

중요: 툴이 채널에 글을 쓰면 그 메시지가 다시 디스패처를 타고
다른 에이전트를 깨운다. 이 순환이 곧 '협업'이다.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.guards import TraceBudget
from app.core.models import (
    Agent, MemberType, Message, MessageKind, Task, TaskStatus, new_id,
)
from app.llm.base import ToolSpec
from app.store.base import Store


@dataclass
class ToolContext:
    """툴이 실행될 때 주어지는 환경."""

    store: Store
    agent: Agent
    channel_id: str
    trigger_message: Message
    budget: TraceBudget
    thread_id: Optional[str] = None
    #: 이 턴에서 새로 만들어진 메시지들. 런타임이 디스패처에 다시 흘려보낸다.
    emitted: List[Message] = field(default_factory=list)
    finished: bool = False
    finish_summary: str = ""

    async def emit(self, text: str, *, thread_id: Optional[str] = None,
                   kind: MessageKind = MessageKind.CHAT) -> Message:
        m = Message(
            id=new_id("msg"),
            channel_id=self.channel_id,
            author_type=MemberType.AGENT,
            author_id=self.agent.id,
            author_name=self.agent.name,
            text=text,
            kind=kind,
            thread_id=thread_id,
            trace_id=self.budget.trace_id,
            depth=self.budget.depth,
            caused_by=self.trigger_message.id,
        )
        await self.store.add_message(m)
        self.emitted.append(m)
        return m


ToolFn = Callable[..., Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: ToolFn
    #: 깊이 한계에 닿으면 이 툴은 목록에서 사라진다 (mention_agent 전용)
    requires_mention_budget: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, name: str, description: str, parameters: Dict[str, Any],
                 *, requires_mention_budget: bool = False):
        def deco(fn: ToolFn) -> ToolFn:
            self._tools[name] = Tool(name, description, parameters, fn,
                                     requires_mention_budget=requires_mention_budget)
            return fn
        return deco

    def available(self, budget: TraceBudget) -> List[Tool]:
        """예산 상태에 따라 실제로 노출할 툴만 골라준다."""
        return [
            t for t in self._tools.values()
            if not (t.requires_mention_budget and not budget.can_mention_agents)
        ]

    def specs(self, budget: TraceBudget) -> List[ToolSpec]:
        return [t.spec() for t in self.available(budget)]

    def names(self, budget: TraceBudget) -> List[str]:
        return [t.name for t in self.available(budget)]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    async def execute(self, name: str, args: Dict[str, Any], ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"오류: '{name}' 도구는 존재하지 않습니다. 사용 가능: {', '.join(self.names(ctx.budget))}"
        if tool.requires_mention_budget and not ctx.budget.can_mention_agents:
            return ("오류: 멘션 깊이 한계에 도달해 다른 에이전트를 부를 수 없습니다. "
                    "지금까지의 결과를 사람에게 직접 보고하고 finish 하세요.")
        try:
            # 스키마에 없는 인자는 버려서 TypeError를 막는다 (LLM이 자주 덧붙임)
            allowed = set(inspect.signature(tool.fn).parameters) - {"ctx"}
            clean = {k: v for k, v in args.items() if k in allowed}
            missing = [
                k for k in tool.parameters.get("required", [])
                if k not in clean or clean[k] in (None, "")
            ]
            if missing:
                return f"오류: 필수 인자 누락 {missing}. 스키마: {tool.parameters}"
            return await tool.fn(ctx=ctx, **clean)
        except Exception as exc:  # 툴 오류는 관측(observation)으로 되돌려 자가수정 유도
            return f"오류: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 기본 툴셋 (Phase 0~2)
# ---------------------------------------------------------------------------
registry = ToolRegistry()

_STR = {"type": "string"}


@registry.register(
    "post_message",
    "채널에 메시지를 올린다. 사람과 다른 에이전트가 볼 수 있는 유일한 방법이다. 3~5문장으로 짧게. 표나 긴 목록은 쓰지 않는다.",
    {"type": "object", "properties": {"text": _STR}, "required": ["text"]},
)
async def post_message(ctx: ToolContext, text: str) -> str:
    m = await ctx.emit(text)
    return f"채널에 게시 완료 (message_id={m.id})"


@registry.register(
    "reply_in_thread",
    "지금 처리 중인 메시지의 스레드에 답글을 단다. 채널 타임라인을 어지럽히지 않는다.",
    {"type": "object", "properties": {"text": _STR}, "required": ["text"]},
)
async def reply_in_thread(ctx: ToolContext, text: str) -> str:
    thread_id = ctx.thread_id or ctx.trigger_message.thread_id or ctx.trigger_message.id
    m = await ctx.emit(text, thread_id=thread_id)
    return f"스레드에 답글 완료 (thread_id={thread_id}, message_id={m.id})"


@registry.register(
    "mention_agent",
    "다른 에이전트를 @멘션해서 일을 넘긴다. 요청은 한두 문장으로 짧게 — 요구사항을 길게 나열하면 상대가 답장만 길어지고 일은 안 한다.",
    {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "에이전트 이름 (@ 없이)"},
            "request": {"type": "string", "description": "무엇을 해달라는지 한두 문장으로"},
        },
        "required": ["agent", "request"],
    },
    requires_mention_budget=True,   # ← 무한 루프를 끊는 지점
)
async def mention_agent(ctx: ToolContext, agent: str, request: str) -> str:
    target = await ctx.store.get_agent_by_name(ctx.agent.workspace_id, agent)
    if target is None:
        return f"오류: '{agent}' 라는 에이전트가 없습니다. list_members로 확인하세요."
    if target.id == ctx.agent.id:
        return "오류: 자기 자신은 멘션할 수 없습니다."
    m = await ctx.emit(f"@{target.name} {request}")
    return f"{target.name} 에게 요청 전달 완료 (message_id={m.id})"


@registry.register(
    "list_members",
    "이 채널에 있는 사람과 에이전트 목록을 본다. 누구에게 일을 넘길지 정할 때 쓴다.",
    {"type": "object", "properties": {}},
)
async def list_members(ctx: ToolContext) -> str:
    members = await ctx.store.channel_members(ctx.channel_id)
    lines = []
    for cm in members:
        if cm.member_type == MemberType.AGENT:
            a = await ctx.store.get_agent(cm.member_id)
            if a and a.id != ctx.agent.id:
                lines.append(f"- @{a.name} (에이전트): {a.role_prompt.splitlines()[0][:70]}")
        else:
            h = await ctx.store.get_human(cm.member_id)
            if h:
                lines.append(f"- {h.name} (사람)")
    return "\n".join(lines) if lines else "(나 혼자입니다)"


@registry.register(
    "search_channel_history",
    "이 채널의 과거 대화를 검색한다. 최근 대화에 없는 배경 정보가 필요할 때 쓴다.",
    {
        "type": "object",
        "properties": {
            "query": _STR,
            "limit": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
)
async def search_channel_history(ctx: ToolContext, query: str, limit: int = 5) -> str:
    hits = await ctx.store.search_messages(ctx.channel_id, query, limit=int(limit or 5))
    if not hits:
        return f"'{query}' 에 대한 과거 대화가 없습니다."
    return "\n".join(f"[{h.author_name}] {h.text[:200]}" for h in hits)


@registry.register(
    "finish",
    "이번 턴에서 할 일을 모두 마쳤을 때 호출한다. 반드시 마지막에 호출해야 한다.",
    {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "무엇을 했는지 한 줄 요약"}},
    },
)
async def finish(ctx: ToolContext, summary: str = "") -> str:
    ctx.finished = True
    ctx.finish_summary = summary
    return "턴을 종료합니다."


# --- Phase 3 자리: 태스크 보드 -------------------------------------------
# create_task / claim_task / update_task_status 를 여기 등록하면
# 런타임·디스패처 수정 없이 그대로 동작한다. 모델(Task)은 이미 정의되어 있다.
