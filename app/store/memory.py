"""의존성 0인 인메모리 저장소. 데모/테스트용이자 Store 계약의 참조 구현."""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Set

from app.core.models import (
    Agent, AgentRun, Channel, ChannelMember, ChannelSummary, Human, MemberType,
    Message, MessageKind, RunStatus, Task, TaskStatus,
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
        self.summaries: Dict[str, ChannelSummary] = {}
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

    async def messages_after(self, channel_id: str, after_ts: Optional[float],
                             limit: int = 200) -> List[Message]:
        out = [self.messages[i] for i in self.channel_index.get(channel_id, [])]
        out = [m for m in out if m.kind == MessageKind.CHAT
               and (after_ts is None or m.created_at > after_ts)]
        return out[:limit]

    async def count_messages_after(self, channel_id: str,
                                   after_ts: Optional[float]) -> int:
        return sum(1 for i in self.channel_index.get(channel_id, [])
                   if self.messages[i].kind == MessageKind.CHAT
                   and (after_ts is None or self.messages[i].created_at > after_ts))

    async def get_summary(self, channel_id: str) -> Optional[ChannelSummary]:
        return self.summaries.get(channel_id)

    async def save_summary(self, summary: ChannelSummary) -> ChannelSummary:
        self.summaries[summary.channel_id] = summary
        return summary

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

    async def add_member(self, member: ChannelMember) -> bool:
        existing = self.members.setdefault(member.channel_id, [])
        if any(x.member_type == member.member_type and x.member_id == member.member_id
               for x in existing):
            return False
        existing.append(member)
        return True

    async def remove_member(self, channel_id: str, member_type: MemberType,
                            member_id: str) -> bool:
        existing = self.members.get(channel_id, [])
        keep = [x for x in existing
                if not (x.member_type == member_type and x.member_id == member_id)]
        self.members[channel_id] = keep
        return len(keep) != len(existing)

    async def get_channel(self, channel_id: str) -> Optional[Channel]:
        return self.channels.get(channel_id)

    async def list_channels(self, workspace_id: str) -> List[Channel]:
        return sorted((c for c in self.channels.values()
                       if c.workspace_id == workspace_id),
                      key=lambda c: c.created_at)

    async def list_agents(self, workspace_id: str) -> List[Agent]:
        return sorted((a for a in self.agents.values()
                       if a.workspace_id == workspace_id),
                      key=lambda a: a.created_at)

    async def update_channel(self, channel_id: str, *, name=None, topic=None):
        ch = self.channels.get(channel_id)
        if ch is None:
            return None
        if name is not None:
            ch.name = name
        if topic is not None:
            ch.topic = topic
        return ch

    async def delete_channel(self, channel_id: str) -> bool:
        if channel_id not in self.channels:
            return False
        for mid in self.channel_index.pop(channel_id, []):
            self.messages.pop(mid, None)
        self.members.pop(channel_id, None)
        self.summaries.pop(channel_id, None)
        for tid in [k for k, v in self.tasks.items() if v.channel_id == channel_id]:
            del self.tasks[tid]
        for rid in [k for k, v in self.runs.items() if v.channel_id == channel_id]:
            self._claimed.discard(self.runs[rid].idem_key)
            del self.runs[rid]
        del self.channels[channel_id]
        return True

    async def update_agent(self, agent_id: str, **fields):
        a = self.agents.get(agent_id)
        if a is None:
            return None
        for k, v in fields.items():
            if v is not None and hasattr(a, k):
                setattr(a, k, v)
        return a

    async def delete_agent(self, agent_id: str) -> bool:
        if agent_id not in self.agents:
            return False
        for cid, lst in self.members.items():
            self.members[cid] = [x for x in lst
                                 if not (x.member_type == MemberType.AGENT
                                         and x.member_id == agent_id)]
        del self.agents[agent_id]
        return True   # 메시지는 그대로 둔다 (append-only)

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

    async def running_runs(self, channel_id: str) -> List[AgentRun]:
        return [r for r in self.runs.values()
                if r.channel_id == channel_id and r.status == RunStatus.RUNNING]

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

    async def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    async def claim_task(self, task_id: str, member_type: MemberType,
                         member_id: str) -> bool:
        async with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.assignee_id is not None:
                return False
            task.assignee_type = member_type
            task.assignee_id = member_id
            if task.status == TaskStatus.TODO:
                task.status = TaskStatus.IN_PROGRESS
            return True

    async def update_task(self, task_id: str, *, status: Optional[TaskStatus] = None,
                          thread_id: Optional[str] = None) -> Optional[Task]:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if status is not None:
            task.status = status
        if thread_id is not None:
            task.thread_id = thread_id
        return task
