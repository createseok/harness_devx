"""프로덕션 저장소 — SQLAlchemy 2.0 async (Postgres).

Store 인터페이스만 구현하면 되므로 런타임/디스패처는 손대지 않는다.
Python 3.11+ 필요.

메시지 테이블이 이 시스템의 진실의 원천(append-only 이벤트 로그)이다.
UPDATE 하지 않는다 — 수정도 새 이벤트로 남긴다.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, case, func, literal, or_, select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core import models as dm
from app.store.base import Store


class Base(DeclarativeBase):
    pass


class AgentRow(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    role_prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reply_mode: Mapped[str] = mapped_column(String(16), default="mention")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    avatar: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    max_steps: Mapped[int] = mapped_column(Integer, default=8)
    created_at: Mapped[float] = mapped_column(Float, default=dm.now_ts)
    # @멘션이 유일하게 해석되려면 워크스페이스 내 이름이 유일해야 한다
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_agent_handle"),)


class HumanRow(Base):
    __tablename__ = "humans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class ChannelRow(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    is_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    topic: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=dm.now_ts)


class MemberRow(Base):
    __tablename__ = "channel_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(64), ForeignKey("channels.id"), index=True)
    member_type: Mapped[str] = mapped_column(String(16))
    member_id: Mapped[str] = mapped_column(String(64))
    reply_mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    __table_args__ = (
        UniqueConstraint("channel_id", "member_type", "member_id", name="uq_member"),
    )


class MessageRow(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    author_type: Mapped[str] = mapped_column(String(16))
    author_id: Mapped[str] = mapped_column(String(64))
    author_name: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="chat")
    thread_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    caused_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=dm.now_ts, index=True)
    __table_args__ = (Index("ix_msg_channel_time", "channel_id", "created_at"),)


class SummaryRow(Base):
    __tablename__ = "channel_summaries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    up_to_message_id: Mapped[str] = mapped_column(String(64))
    up_to_created_at: Mapped[float] = mapped_column(Float)
    covered_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=dm.now_ts, index=True)
    # 채널당 여러 세대를 남긴다 — 요약이 잘못 생성됐을 때 이전 세대로 돌아갈 수 있다
    __table_args__ = (Index("ix_summary_channel_time", "channel_id", "created_at"),)


class RunRow(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    trigger_message_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="running")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=dm.now_ts)
    # ★ 멱등성의 핵심. DB 유니크 제약이 중복 실행을 원자적으로 막는다.
    __table_args__ = (
        UniqueConstraint("agent_id", "trigger_message_id", name="uq_run_idem"),
    )


class TaskRow(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="todo")
    assignee_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    assignee_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=dm.now_ts)


# --- 행 ↔ 도메인 모델 변환 -------------------------------------------------
def _msg(r: MessageRow) -> dm.Message:
    return dm.Message(
        id=r.id, channel_id=r.channel_id, author_type=dm.MemberType(r.author_type),
        author_id=r.author_id, author_name=r.author_name, text=r.text,
        kind=dm.MessageKind(r.kind), thread_id=r.thread_id, trace_id=r.trace_id,
        depth=r.depth, caused_by=r.caused_by, created_at=r.created_at,
    )


def _task(r: "TaskRow") -> dm.Task:
    return dm.Task(
        id=r.id, channel_id=r.channel_id, title=r.title, description=r.description,
        status=dm.TaskStatus(r.status),
        assignee_type=dm.MemberType(r.assignee_type) if r.assignee_type else None,
        assignee_id=r.assignee_id, thread_id=r.thread_id, created_at=r.created_at,
    )


def _agent(r: AgentRow) -> dm.Agent:
    return dm.Agent(
        id=r.id, workspace_id=r.workspace_id, name=r.name, role_prompt=r.role_prompt,
        model=r.model, reply_mode=dm.ReplyMode(r.reply_mode), enabled=r.enabled,
        avatar=r.avatar, max_steps=r.max_steps, created_at=r.created_at,
    )


class SqlStore(Store):
    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.engine = create_async_engine(database_url, echo=echo, pool_pre_ping=True)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # --- 메시지 ---
    async def add_message(self, message: dm.Message) -> dm.Message:
        async with self.session() as s, s.begin():
            s.add(MessageRow(
                id=message.id, channel_id=message.channel_id,
                author_type=message.author_type.value, author_id=message.author_id,
                author_name=message.author_name, text=message.text,
                kind=message.kind.value, thread_id=message.thread_id,
                trace_id=message.trace_id, depth=message.depth,
                caused_by=message.caused_by, created_at=message.created_at,
            ))
        return message

    async def get_message(self, message_id: str) -> Optional[dm.Message]:
        async with self.session() as s:
            r = await s.get(MessageRow, message_id)
            return _msg(r) if r else None

    async def recent_messages(self, channel_id, limit=50, *, include_tool_logs=False):
        async with self.session() as s:
            q = select(MessageRow).where(MessageRow.channel_id == channel_id)
            if not include_tool_logs:
                q = q.where(MessageRow.kind != dm.MessageKind.TOOL_LOG.value)
            q = q.order_by(MessageRow.created_at.desc()).limit(limit)
            rows = (await s.execute(q)).scalars().all()
            return [_msg(r) for r in reversed(rows)]

    async def thread_messages(self, thread_id: str, limit: int = 50):
        async with self.session() as s:
            q = (select(MessageRow)
                 .where(or_(MessageRow.thread_id == thread_id, MessageRow.id == thread_id))
                 .order_by(MessageRow.created_at).limit(limit))
            return [_msg(r) for r in (await s.execute(q)).scalars().all()]

    async def search_messages(self, channel_id: str, query: str, limit: int = 10):
        """토큰별로 쪼개서 일치 개수로 점수를 매긴다.

        통짜 ILIKE '%결제 실패율%' 는 그 어절이 통째로 붙어 있어야만 맞는다.
        실제 질의는 대개 여러 단어이고 문서에는 흩어져 있으므로 거의 못 찾는다.

        한국어 형태소 분석기가 없는 Postgres 기본 설정에서는 to_tsvector 도
        실효가 없다. 토큰 단위 ILIKE 스코어링이 현실적인 절충이다.
        pgvector 로 갈 때는 이 메서드만 교체하면 된다 (인터페이스 그대로).
        """
        terms = [t for t in (query or "").split() if len(t) >= 2][:8]
        if not terms:
            return []
        async with self.session() as s:
            score = sum(
                (case((MessageRow.text.ilike(f"%{t}%"), 1), else_=0) for t in terms),
                literal(0),
            ).label("score")
            q = (select(MessageRow, score)
                 .where(MessageRow.channel_id == channel_id,
                        MessageRow.kind == dm.MessageKind.CHAT.value,
                        or_(*[MessageRow.text.ilike(f"%{t}%") for t in terms]))
                 .order_by(score.desc(), MessageRow.created_at.desc())
                 .limit(limit))
            return [_msg(row[0]) for row in (await s.execute(q)).all()]

    async def messages_after(self, channel_id: str, after_ts, limit: int = 200):
        async with self.session() as s:
            q = select(MessageRow).where(
                MessageRow.channel_id == channel_id,
                MessageRow.kind == dm.MessageKind.CHAT.value)
            if after_ts is not None:
                q = q.where(MessageRow.created_at > after_ts)
            q = q.order_by(MessageRow.created_at).limit(limit)
            return [_msg(r) for r in (await s.execute(q)).scalars().all()]

    async def count_messages_after(self, channel_id: str, after_ts) -> int:
        async with self.session() as s:
            q = select(func.count(MessageRow.id)).where(
                MessageRow.channel_id == channel_id,
                MessageRow.kind == dm.MessageKind.CHAT.value)
            if after_ts is not None:
                q = q.where(MessageRow.created_at > after_ts)
            return int((await s.execute(q)).scalar() or 0)

    # --- 요약 ---
    async def get_summary(self, channel_id: str):
        async with self.session() as s:
            q = (select(SummaryRow).where(SummaryRow.channel_id == channel_id)
                 .order_by(SummaryRow.created_at.desc()).limit(1))
            r = (await s.execute(q)).scalars().first()
            if r is None:
                return None
            return dm.ChannelSummary(
                id=r.id, channel_id=r.channel_id, text=r.text,
                up_to_message_id=r.up_to_message_id,
                up_to_created_at=r.up_to_created_at,
                covered_count=r.covered_count, created_at=r.created_at)

    async def save_summary(self, summary: dm.ChannelSummary):
        async with self.session() as s, s.begin():
            s.add(SummaryRow(
                id=summary.id, channel_id=summary.channel_id, text=summary.text,
                up_to_message_id=summary.up_to_message_id,
                up_to_created_at=summary.up_to_created_at,
                covered_count=summary.covered_count, created_at=summary.created_at))
        return summary

    # --- 멤버 ---
    async def get_agent(self, agent_id: str) -> Optional[dm.Agent]:
        async with self.session() as s:
            r = await s.get(AgentRow, agent_id)
            return _agent(r) if r else None

    async def get_agent_by_name(self, workspace_id: str, name: str) -> Optional[dm.Agent]:
        async with self.session() as s:
            q = select(AgentRow).where(
                AgentRow.workspace_id == workspace_id,
                func.lower(AgentRow.name) == name.lstrip("@").lower(),
            )
            r = (await s.execute(q)).scalars().first()
            return _agent(r) if r else None

    async def get_human(self, human_id: str) -> Optional[dm.Human]:
        async with self.session() as s:
            r = await s.get(HumanRow, human_id)
            return dm.Human(r.id, r.workspace_id, r.name, r.email) if r else None

    async def channel_members(self, channel_id: str):
        async with self.session() as s:
            q = select(MemberRow).where(MemberRow.channel_id == channel_id)
            return [
                dm.ChannelMember(
                    r.channel_id, dm.MemberType(r.member_type), r.member_id,
                    dm.ReplyMode(r.reply_mode) if r.reply_mode else None,
                )
                for r in (await s.execute(q)).scalars().all()
            ]

    async def add_member(self, member: dm.ChannelMember) -> bool:
        from sqlalchemy.exc import IntegrityError
        try:
            async with self.session() as s, s.begin():
                s.add(MemberRow(
                    channel_id=member.channel_id,
                    member_type=member.member_type.value,
                    member_id=member.member_id,
                    reply_mode=member.reply_mode.value if member.reply_mode else None))
            return True
        except IntegrityError:
            return False   # UNIQUE(channel_id, member_type, member_id)

    async def remove_member(self, channel_id: str, member_type: dm.MemberType,
                            member_id: str) -> bool:
        from sqlalchemy import delete
        async with self.session() as s, s.begin():
            r = await s.execute(delete(MemberRow).where(
                MemberRow.channel_id == channel_id,
                MemberRow.member_type == member_type.value,
                MemberRow.member_id == member_id))
            return r.rowcount > 0

    async def get_channel(self, channel_id: str) -> Optional[dm.Channel]:
        async with self.session() as s:
            r = await s.get(ChannelRow, channel_id)
            return dm.Channel(r.id, r.workspace_id, r.name, r.is_dm, r.topic,
                              r.created_at) if r else None

    async def list_channels(self, workspace_id: str):
        async with self.session() as s:
            rows = (await s.execute(
                select(ChannelRow).where(ChannelRow.workspace_id == workspace_id)
                .order_by(ChannelRow.created_at))).scalars().all()
            return [dm.Channel(r.id, r.workspace_id, r.name, r.is_dm, r.topic,
                               r.created_at) for r in rows]

    async def list_agents(self, workspace_id: str):
        async with self.session() as s:
            rows = (await s.execute(
                select(AgentRow).where(AgentRow.workspace_id == workspace_id)
                .order_by(AgentRow.created_at))).scalars().all()
            return [_agent(r) for r in rows]

    # --- 실행 기록 ---
    async def claim_run(self, run: dm.AgentRun) -> bool:
        """UNIQUE(agent_id, trigger_message_id) 위반 = 이미 실행됨.

        DB가 원자성을 보장하므로 워커가 여러 프로세스여도 안전하다.
        """
        from sqlalchemy.exc import IntegrityError
        try:
            async with self.session() as s, s.begin():
                s.add(RunRow(
                    id=run.id, agent_id=run.agent_id, channel_id=run.channel_id,
                    trigger_message_id=run.trigger_message_id, trace_id=run.trace_id,
                    depth=run.depth, status=run.status.value, created_at=run.created_at,
                ))
            return True
        except IntegrityError:
            return False

    async def finish_run(self, run: dm.AgentRun) -> None:
        async with self.session() as s, s.begin():
            r = await s.get(RunRow, run.id)
            if r is None:
                return
            r.status = run.status.value
            r.steps = run.steps
            r.prompt_tokens = run.prompt_tokens
            r.completion_tokens = run.completion_tokens
            r.error = run.error

    async def running_runs(self, channel_id: str):
        async with self.session() as s:
            rows = (await s.execute(
                select(RunRow).where(RunRow.channel_id == channel_id,
                                     RunRow.status == dm.RunStatus.RUNNING.value)
                .order_by(RunRow.created_at))).scalars().all()
            return [dm.AgentRun(
                id=r.id, agent_id=r.agent_id, channel_id=r.channel_id,
                trigger_message_id=r.trigger_message_id, trace_id=r.trace_id,
                depth=r.depth, status=dm.RunStatus(r.status), steps=r.steps,
                created_at=r.created_at) for r in rows]

    async def trace_usage(self, trace_id: str) -> int:
        async with self.session() as s:
            q = select(
                func.coalesce(func.sum(RunRow.prompt_tokens + RunRow.completion_tokens), 0)
            ).where(RunRow.trace_id == trace_id)
            return int((await s.execute(q)).scalar() or 0)

    # --- 태스크 ---
    async def add_task(self, task: dm.Task) -> dm.Task:
        async with self.session() as s, s.begin():
            s.add(TaskRow(
                id=task.id, channel_id=task.channel_id, title=task.title,
                description=task.description, status=task.status.value,
                assignee_type=task.assignee_type.value if task.assignee_type else None,
                assignee_id=task.assignee_id, thread_id=task.thread_id,
                created_at=task.created_at,
            ))
        return task

    async def get_task(self, task_id: str):
        async with self.session() as s:
            r = await s.get(TaskRow, task_id)
            return _task(r) if r else None

    async def claim_task(self, task_id: str, member_type: dm.MemberType,
                         member_id: str) -> bool:
        """담당자가 비어 있을 때만 UPDATE 한다.

        WHERE assignee_id IS NULL 조건이 원자성을 만든다. 애플리케이션에서
        읽고-검사하고-쓰면 두 워커가 동시에 통과할 수 있다.
        """
        from sqlalchemy import update
        async with self.session() as s, s.begin():
            stmt = (
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.assignee_id.is_(None))
                .values(assignee_type=member_type.value, assignee_id=member_id,
                        status=dm.TaskStatus.IN_PROGRESS.value)
            )
            result = await s.execute(stmt)
            return result.rowcount == 1

    async def update_task(self, task_id: str, *, status=None, thread_id=None):
        async with self.session() as s, s.begin():
            r = await s.get(TaskRow, task_id)
            if r is None:
                return None
            if status is not None:
                r.status = status.value
            if thread_id is not None:
                r.thread_id = thread_id
            await s.flush()
            return _task(r)

    async def list_tasks(self, channel_id: str):
        async with self.session() as s:
            q = select(TaskRow).where(TaskRow.channel_id == channel_id)
            return [_task(r) for r in (await s.execute(q)).scalars().all()]
