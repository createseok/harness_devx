"""엔진: 메시지 → 디스패처 → 에이전트 턴 → 새 메시지 → 디스패처 … 의 순환.

이 순환이 '협업'의 전부다. 별도의 오케스트레이션 그래프는 없다.

프로덕션 전환 지점:
    지금은 asyncio.Queue 를 쓴다. 여러 워커 프로세스로 확장할 때는
    `_queue` 를 Redis Streams(consumer group)로 바꾸고
    `_locks` 를 Redis 분산 락으로 바꾸면 나머지는 그대로다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from app.core.guards import TraceBudget, TurnGate
from app.core.models import Channel, Message, MessageKind, new_id
from app.core.router import Router
from app.core.runtime import AgentRuntime, TurnResult
from app.core.tools import ToolRegistry
from app.llm.base import LLMProvider
from app.store.base import Store

log = logging.getLogger(__name__)


@dataclass
class Job:
    agent_id: str
    channel_id: str
    trigger_message_id: str
    budget: TraceBudget


@dataclass
class EngineStats:
    turns: int = 0
    messages: int = 0
    skipped: List[str] = field(default_factory=list)
    tokens: int = 0


class Engine:
    def __init__(
        self,
        store: Store,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        max_concurrency: int = 4,
        default_budget: Optional[TraceBudget] = None,
        on_message: Optional[Callable[[Message], None]] = None,
        gate: Optional[TurnGate] = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.registry = registry
        self.gate = gate or TurnGate()
        self.router = Router(store, self.gate)
        self.runtime = AgentRuntime(store, provider, registry)
        self._queue: "asyncio.Queue[Optional[Job]]" = asyncio.Queue()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(max_concurrency)
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._workers: List[asyncio.Task] = []
        self._max_concurrency = max_concurrency
        self._default_budget = default_budget
        self.on_message = on_message
        self.stats = EngineStats()

    # --- 라이프사이클 ---
    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.ensure_future(self._worker(i)) for i in range(self._max_concurrency)
        ]

    async def stop(self) -> None:
        for _ in self._workers:
            await self._queue.put(None)
        for w in self._workers:
            await w
        self._workers = []

    async def wait_idle(self) -> None:
        """큐가 비고 실행 중인 턴도 없을 때까지 기다린다 (데모/테스트용)."""
        await self._queue.join()
        await self._idle.wait()

    # --- 입력 ---
    async def submit(self, message: Message, budget: Optional[TraceBudget] = None) -> None:
        """새 메시지를 시스템에 넣는다. 사람의 발화든 에이전트의 발화든 동일하다."""
        if not message.trace_id:
            message.trace_id = new_id("trace")
        if budget is None:
            base = self._default_budget
            budget = TraceBudget(
                trace_id=message.trace_id,
                depth=message.depth,
                max_depth=base.max_depth if base else 4,
                max_tokens=base.max_tokens if base else 120_000,
                max_runs=base.max_runs if base else 20,
            )
            if base:
                budget.tokens_spent = base.tokens_spent
                budget.runs_spent = base.runs_spent

        # 진입점에서 영속화한다. 툴이 emit한 메시지는 이미 저장되어 있으므로
        # 중복 저장하지 않는다.
        if await self.store.get_message(message.id) is None:
            await self.store.add_message(message)

        if self.on_message and message.kind == MessageKind.CHAT:
            self.on_message(message)
        self.stats.messages += 1

        await self._dispatch(message, budget)

    async def _dispatch(self, message: Message, budget: TraceBudget) -> None:
        result = await self.router.route(message, budget)
        for name, reason in result.skipped:
            entry = f"{name}: {reason}"
            self.stats.skipped.append(entry)
            log.info("깨우지 않음 — %s", entry)

        for target in result.targets:
            self.gate.commit(budget.trace_id, target.agent.id)
            self._inflight += 1
            self._idle.clear()
            await self._queue.put(Job(
                agent_id=target.agent.id,
                channel_id=message.channel_id,
                trigger_message_id=message.id,
                budget=budget,
            ))

    # --- 워커 ---
    async def _worker(self, idx: int) -> None:
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            try:
                await self._run_job(job)
            except Exception:
                log.exception("워커 %s 에서 예외", idx)
            finally:
                self._inflight -= 1
                if self._inflight <= 0:
                    self._idle.set()
                self._queue.task_done()

    def _lock_for(self, agent_id: str, channel_id: str) -> asyncio.Lock:
        # (에이전트, 채널) 당 동시 실행 1개.
        # 이게 없으면 에이전트가 자기 말에 자기가 답하거나 두 턴이 뒤엉킨다.
        key = f"{agent_id}:{channel_id}"
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _run_job(self, job: Job) -> None:
        agent = await self.store.get_agent(job.agent_id)
        channel = await self.store.get_channel(job.channel_id)
        trigger = await self.store.get_message(job.trigger_message_id)
        if not (agent and channel and trigger):
            return

        async with self._sem:
            async with self._lock_for(job.agent_id, job.channel_id):
                result = await self.runtime.run_turn(agent, channel, trigger, job.budget)

        if result is None:   # 멱등성으로 걸러진 중복 실행
            return

        self.stats.turns += 1
        self.stats.tokens += result.run.prompt_tokens + result.run.completion_tokens
        for w in result.warnings:
            log.info("[%s] %s", agent.name, w)

        # 이 턴이 만든 메시지를 다시 시스템에 흘려보낸다 → 순환 완성
        child = job.budget.child()
        for msg in result.emitted:
            if msg.kind != MessageKind.CHAT:
                continue
            if self.on_message:
                self.on_message(msg)
            self.stats.messages += 1
            await self._dispatch(msg, child)
