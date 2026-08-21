"""의존성 0인 인메모리 저장소. 데모/테스트용이자 Store 계약의 참조 구현."""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Set

from app.core.models import (
    Agent, AgentRun, Channel, ChannelMember, Human, Message, MessageKind, Task,
)
from app.store.base import Store


class InMemoryStore(Store):
    def __init__(self) -> None:
        self.messages: Dict[str, Message] = {}
        self.channel_index: Dict[str, List[str]] = {}
        self.agents: Dict[str, Agent] = {}
        self.humans: Dict[str, Human] = {}
        self.channels: Dict[str, Channel] = {}
        self.members: Dict[str, List[ChannelMember]] = {}
        self.runs: Dict[str, AgentRun] = {}
        self._claimed: Set[str] = set()
        self.tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()

    # --- 시드 헬퍼 ---
    def put_agent(self, agent: Agent) -> Agent:
        self.agents[agent.id] = agent
        return agent

    def put_human(self, human: Human) -> Human:
        self.humans[human.id] = human
        return human

    def put_channel(self, channel: Channel) -> Channel:
        self.channels[channel.id] = channel
        self.members.setdefault(channel.id, [])
        self.channel_index.setdefault(channel.id, [])
        return channel

    def join(self, member: ChannelMember) -> None:
        self.members.setdefault(member.channel_id, []).append(member)

    # --- 메시지 ---
    async def add_message(self, message: Message) -> Message:
        async with self._lock:
            self.messages[message.id] = message
            self.channel_index.setdefault(message.channel_id, []).append(message.id)
        return message

    async def get_message(self, message_id: str) -> Optional[Message]:
        return self.messages.get(message_id)

    async def recent_messages(
        self, channel_id: str, limit: int = 50, *, include_tool_logs: bool = False
    ) -> List[Message]:
        ids = self.channel_index.get(channel_id, [])
        out = [self.messages[i] for i in ids]
        if not include_tool_logs:
            out = [m for m in out if m.kind != MessageKind.TOOL_LOG]
        return out[-limit:]

    async def thread_messages(self, thread_id: str, limit: int = 50) -> List[Message]:
        out = [m for m in self.messages.values()
               if m.thread_id == thread_id or m.id == thread_id]
        out.sort(key=lambda m: m.created_at)
        return out[-limit:]

    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> List[Message]:
        # 프로덕션에서는 BM25 + pgvector 하이브리드. 여기서는 단순 토큰 매칭.
        terms = [t for t in query.lower().split() if t]
        scored = []
        for mid in self.channel_index.get(channel_id, []):
            m = self.messages[mid]
            if m.kind == MessageKind.TOOL_LOG:
                continue
            body = m.text.lower()
            score = sum(1 for t in terms if t in body)
            if score:
                scored.append((score, m.created_at, m))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [m for _, _, m in scored[:limit]]

    # --- 멤버 ---
    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self.agents.get(agent_id)

    async def get_agent_by_name(self, workspace_id: str, name: str) -> Optional[Agent]:
        target = name.lstrip("@").lower()
        for a in self.agents.values():
            if a.workspace_id == workspace_id and a.name.lower() == target:
                return a
        return None

    async def get_human(self, human_id: str) -> Optional[Human]:
        return self.humans.get(human_id)

    async def channel_members(self, channel_id: str) -> List[ChannelMember]:
        return list(self.members.get(channel_id, []))

    async def get_channel(self, channel_id: str) -> Optional[Channel]:
        return self.channels.get(channel_id)

    # --- 실행 기록 ---
    async def claim_run(self, run: AgentRun) -> bool:
        async with self._lock:
            if run.idem_key in self._claimed:
                return False
            self._claimed.add(run.idem_key)
            self.runs[run.id] = run
            return True

    async def finish_run(self, run: AgentRun) -> None:
        self.runs[run.id] = run

    async def trace_usage(self, trace_id: str) -> int:
        return sum(
            r.prompt_tokens + r.completion_tokens
            for r in self.runs.values()
            if r.trace_id == trace_id
        )

    # --- 태스크 ---
    async def add_task(self, task: Task) -> Task:
        self.tasks[task.id] = task
        return task

    async def list_tasks(self, channel_id: str) -> List[Task]:
        return [t for t in self.tasks.values() if t.channel_id == channel_id]
