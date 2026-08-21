"""저장소 인터페이스.

에이전트 런타임은 이 인터페이스만 안다. 덕분에
- 테스트/데모는 InMemoryStore (의존성 0)
- 프로덕션은 SqlStore (Postgres + pgvector)
로 갈아끼울 수 있다.
"""
from __future__ import annotations

import abc
from typing import List, Optional

from app.core.models import (
    Agent, AgentRun, Channel, ChannelMember, Human, Message, Task,
)


class Store(abc.ABC):
    # --- 메시지 (append-only 이벤트 로그) ---
    @abc.abstractmethod
    async def add_message(self, message: Message) -> Message: ...

    @abc.abstractmethod
    async def get_message(self, message_id: str) -> Optional[Message]: ...

    @abc.abstractmethod
    async def recent_messages(
        self, channel_id: str, limit: int = 50, *, include_tool_logs: bool = False
    ) -> List[Message]: ...

    @abc.abstractmethod
    async def thread_messages(self, thread_id: str, limit: int = 50) -> List[Message]: ...

    @abc.abstractmethod
    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> List[Message]: ...

    # --- 멤버 ---
    @abc.abstractmethod
    async def get_agent(self, agent_id: str) -> Optional[Agent]: ...

    @abc.abstractmethod
    async def get_agent_by_name(self, workspace_id: str, name: str) -> Optional[Agent]: ...

    @abc.abstractmethod
    async def get_human(self, human_id: str) -> Optional[Human]: ...

    @abc.abstractmethod
    async def channel_members(self, channel_id: str) -> List[ChannelMember]: ...

    @abc.abstractmethod
    async def get_channel(self, channel_id: str) -> Optional[Channel]: ...

    # --- 실행 기록 (멱등성 + 과금) ---
    @abc.abstractmethod
    async def claim_run(self, run: AgentRun) -> bool:
        """(agent_id, trigger_message_id)를 선점한다.

        이미 있으면 False → 중복 실행을 막는다. 원자적이어야 한다.
        """

    @abc.abstractmethod
    async def finish_run(self, run: AgentRun) -> None: ...

    @abc.abstractmethod
    async def trace_usage(self, trace_id: str) -> int:
        """해당 trace에서 지금까지 쓴 총 토큰. 예산 가드가 사용한다."""

    # --- 태스크 (Phase 3) ---
    @abc.abstractmethod
    async def add_task(self, task: Task) -> Task: ...

    @abc.abstractmethod
    async def list_tasks(self, channel_id: str) -> List[Task]: ...
