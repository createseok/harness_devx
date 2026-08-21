"""FastAPI 레이어. Python 3.11+ 필요.

    uvicorn app.api.main:app --reload

구조상 API는 얇다 — 메시지를 저장하고 엔진에 넣으면 나머지는 알아서 굴러간다.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.core.bus import bus
from app.core.engine import Engine
from app.core.guards import TraceBudget
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, ReplyMode,
    Task, TaskStatus, new_id,
)
from app.core.router import MENTION_RE
from app.core.tools import registry
from app.llm.base import LLMProvider
from app.store.base import Store

state: dict = {}


def build_provider() -> LLMProvider:
    """LLM_PROVIDER 환경변수 하나로 백엔드가 결정된다."""
    from app.llm.registry import build_provider as _build
    return _build()


async def build_store() -> Store:
    if settings.use_memory_store:
        from app.store.memory import InMemoryStore
        return InMemoryStore()
    from app.store.sql import SqlStore
    store = SqlStore(settings.database_url)
    await store.create_all()
    return store


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = await build_store()
    provider = build_provider()

    def publish(msg: Message) -> None:
        bus.publish(msg.channel_id, _msg_dict(msg))

    engine = Engine(
        store, provider, registry,
        max_concurrency=settings.max_concurrency,
        default_budget=TraceBudget(
            trace_id="",
            max_depth=settings.max_mention_depth,
            max_tokens=settings.max_trace_tokens,
            max_runs=settings.max_trace_runs,
        ),
        on_message=publish,
    )
    await engine.start()
    state.update(store=store, provider=provider, engine=engine)
    try:
        yield
    finally:
        await engine.stop()
        await provider.aclose()


app = FastAPI(title="GenTeam-like Agent Workspace", lifespan=lifespan)


def get_store() -> Store:
    return state["store"]


def get_engine() -> Engine:
    return state["engine"]


def _msg_dict(m: Message) -> dict:
    return {
        "id": m.id, "channel_id": m.channel_id, "author_type": m.author_type.value,
        "author_id": m.author_id, "author_name": m.author_name, "text": m.text,
        "kind": m.kind.value, "thread_id": m.thread_id, "trace_id": m.trace_id,
        "depth": m.depth, "created_at": m.created_at,
    }


# --- 스키마 ---
class CreateAgent(BaseModel):
    workspace_id: str
    name: str = Field(..., description="@멘션 핸들. 워크스페이스 내 유일해야 함")
    role_prompt: str
    model: Optional[str] = None
    reply_mode: ReplyMode = ReplyMode.MENTION
    max_steps: int = Field(8, ge=1, le=20)

    @field_validator("name")
    @classmethod
    def valid_handle(cls, v: str) -> str:
        """디스패처의 MENTION_RE 와 같은 문자만 허용한다.

        공백이나 특수문자가 들어가면 '@김 기획' 이 '@김' 까지만 파싱돼
        영영 호출되지 않는 에이전트가 만들어진다.
        """
        v = v.strip().lstrip("@")
        if not v:
            raise ValueError("이름이 비어 있습니다")
        if not MENTION_RE.fullmatch("@" + v):
            raise ValueError(
                "이름에는 한글·영문·숫자·밑줄·하이픈만 쓸 수 있습니다 "
                "(공백 불가 — @멘션으로 부를 수 없게 됩니다)")
        return v

    @field_validator("role_prompt")
    @classmethod
    def has_role(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("역할 설명을 10자 이상 적어주세요. "
                             "이게 에이전트의 행동을 결정합니다.")
        return v


class CreateHuman(BaseModel):
    workspace_id: str
    name: str
    email: Optional[str] = None


class CreateChannel(BaseModel):
    workspace_id: str
    name: str
    topic: str = ""
    is_dm: bool = False


class AddMember(BaseModel):
    member_type: MemberType
    member_id: str
    reply_mode: Optional[ReplyMode] = None


class CreateTask(BaseModel):
    title: str
    description: str = ""
    assignee_type: Optional[MemberType] = None
    assignee_id: Optional[str] = None


class UpdateTask(BaseModel):
    status: TaskStatus


class PostMessage(BaseModel):
    author_id: str
    author_name: str
    text: str
    thread_id: Optional[str] = None
    author_type: MemberType = MemberType.HUMAN


# --- 엔드포인트 ---
UI_FILE = Path(__file__).parent / "static" / "index.html"


@app.get("/")
async def index():
    return FileResponse(UI_FILE)


@app.get("/healthz")
async def healthz():
    provider = state.get("provider")
    return {
        "ok": True,
        "provider": settings.llm_provider,
        "model": getattr(provider, "default_model", None),
        "native_tools": getattr(provider, "supports_native_tools", None),
    }


@app.post("/api/agents")
async def create_agent(body: CreateAgent, store: Store = Depends(get_store)):
    if await store.get_agent_by_name(body.workspace_id, body.name):
        raise HTTPException(409, f"'{body.name}' 핸들이 이미 사용 중입니다")
    agent = Agent(id=new_id("agt"), **body.model_dump())
    if hasattr(store, "put_agent"):
        store.put_agent(agent)
    else:
        from app.store.sql import AgentRow
        async with store.session() as s, s.begin():
            s.add(AgentRow(
                id=agent.id, workspace_id=agent.workspace_id, name=agent.name,
                role_prompt=agent.role_prompt, model=agent.model,
                reply_mode=agent.reply_mode.value, enabled=agent.enabled,
                max_steps=agent.max_steps, created_at=agent.created_at,
            ))
    return agent


@app.get("/api/channels")
async def list_channels(workspace_id: str = "ws_demo",
                        store: Store = Depends(get_store)):
    return [
        {"id": c.id, "name": c.name, "topic": c.topic, "is_dm": c.is_dm}
        for c in await store.list_channels(workspace_id)
    ]


@app.get("/api/agents")
async def list_agents(workspace_id: str = "ws_demo",
                      store: Store = Depends(get_store)):
    return [
        {"id": a.id, "name": a.name, "role_prompt": a.role_prompt,
         "reply_mode": a.reply_mode.value, "enabled": a.enabled,
         "model": a.model}
        for a in await store.list_agents(workspace_id)
    ]


@app.post("/api/humans")
async def create_human(body: CreateHuman, store: Store = Depends(get_store)):
    human = Human(id=new_id("usr"), **body.model_dump())
    if hasattr(store, "put_human"):
        store.put_human(human)
    else:
        from app.store.sql import HumanRow
        async with store.session() as s, s.begin():
            s.add(HumanRow(id=human.id, workspace_id=human.workspace_id,
                           name=human.name, email=human.email))
    return human


@app.get("/api/channels/{channel_id}/members")
async def list_members(channel_id: str, store: Store = Depends(get_store)):
    """채널의 사람 + 에이전트 로스터."""
    out = []
    for cm in await store.channel_members(channel_id):
        if cm.member_type == MemberType.AGENT:
            a = await store.get_agent(cm.member_id)
            if a:
                out.append({"type": "agent", "id": a.id, "name": a.name,
                            "reply_mode": (cm.reply_mode or a.reply_mode).value,
                            "role": a.role_prompt.splitlines()[0][:120]})
        else:
            h = await store.get_human(cm.member_id)
            if h:
                out.append({"type": "human", "id": h.id, "name": h.name})
    return out


@app.post("/api/channels")
async def create_channel(body: CreateChannel, store: Store = Depends(get_store)):
    ch = Channel(id=new_id("ch"), **body.model_dump())
    if hasattr(store, "put_channel"):
        store.put_channel(ch)
    else:
        from app.store.sql import ChannelRow
        async with store.session() as s, s.begin():
            s.add(ChannelRow(id=ch.id, workspace_id=ch.workspace_id, name=ch.name,
                             is_dm=ch.is_dm, topic=ch.topic, created_at=ch.created_at))
    return ch


@app.post("/api/channels/{channel_id}/members")
async def add_member(channel_id: str, body: AddMember, store: Store = Depends(get_store)):
    if not await store.get_channel(channel_id):
        raise HTTPException(404, "채널이 없습니다")
    if body.member_type == MemberType.AGENT:
        if not await store.get_agent(body.member_id):
            raise HTTPException(404, "에이전트가 없습니다")
    elif not await store.get_human(body.member_id):
        raise HTTPException(404, "사용자가 없습니다")

    added = await store.add_member(
        ChannelMember(channel_id, body.member_type, body.member_id, body.reply_mode))
    return {"ok": True, "added": added}   # added=False 면 이미 멤버였다는 뜻


@app.delete("/api/channels/{channel_id}/members/{member_type}/{member_id}")
async def remove_member(channel_id: str, member_type: MemberType, member_id: str,
                        store: Store = Depends(get_store)):
    if not await store.remove_member(channel_id, member_type, member_id):
        raise HTTPException(404, "채널에 없는 멤버입니다")
    return {"ok": True}


@app.get("/api/channels/{channel_id}/messages")
async def list_messages(channel_id: str, limit: int = 50, include_tool_logs: bool = False,
                        store: Store = Depends(get_store)):
    msgs = await store.recent_messages(channel_id, limit=limit,
                                       include_tool_logs=include_tool_logs)
    return [_msg_dict(m) for m in msgs]


@app.post("/api/channels/{channel_id}/messages")
async def post_message(channel_id: str, body: PostMessage,
                       store: Store = Depends(get_store),
                       engine: Engine = Depends(get_engine)):
    """사람이 메시지를 올린다. 여기서부터 에이전트 연쇄가 시작된다.

    엔진에 넣기만 하고 바로 응답한다 — 에이전트 작업은 백그라운드에서 돌고
    결과는 SSE 로 흘러나온다.
    """
    if not await store.get_channel(channel_id):
        raise HTTPException(404, "채널이 없습니다")
    msg = Message(
        id=new_id("msg"), channel_id=channel_id, author_type=body.author_type,
        author_id=body.author_id, author_name=body.author_name, text=body.text,
        thread_id=body.thread_id, trace_id=new_id("trace"),
    )
    asyncio.ensure_future(engine.submit(msg))
    return _msg_dict(msg)


@app.get("/api/channels/{channel_id}/activity")
async def channel_activity(channel_id: str, store: Store = Depends(get_store)):
    """지금 일하고 있는 에이전트. claude -p 는 호출당 1~2분이라
    이 표시가 없으면 사용자는 멈춘 것인지 도는 것인지 알 수 없다."""
    out = []
    for r in await store.running_runs(channel_id):
        a = await store.get_agent(r.agent_id)
        out.append({"agent_id": r.agent_id, "name": a.name if a else r.agent_id,
                    "steps": r.steps, "depth": r.depth,
                    "started_at": r.created_at})
    return {"working": out}


@app.get("/api/channels/{channel_id}/tasks")
async def list_tasks(channel_id: str, store: Store = Depends(get_store)):
    """태스크 보드. 칸반 컬럼별로 묶어서 돌려준다."""
    tasks = await store.list_tasks(channel_id)
    board = {s.value: [] for s in TaskStatus}
    for t in sorted(tasks, key=lambda x: x.created_at):
        assignee = None
        if t.assignee_id:
            if t.assignee_type == MemberType.AGENT:
                a = await store.get_agent(t.assignee_id)
                assignee = {"type": "agent", "id": t.assignee_id,
                            "name": a.name if a else None}
            else:
                h = await store.get_human(t.assignee_id)
                assignee = {"type": "human", "id": t.assignee_id,
                            "name": h.name if h else None}
        board[t.status.value].append({
            "id": t.id, "title": t.title, "description": t.description,
            "assignee": assignee, "thread_id": t.thread_id,
            "created_at": t.created_at,
        })
    return board


@app.post("/api/channels/{channel_id}/tasks")
async def create_task(channel_id: str, body: CreateTask,
                      store: Store = Depends(get_store)):
    if not await store.get_channel(channel_id):
        raise HTTPException(404, "채널이 없습니다")
    task = Task(
        id=new_id("tsk"), channel_id=channel_id, title=body.title,
        description=body.description, assignee_type=body.assignee_type,
        assignee_id=body.assignee_id,
        status=TaskStatus.IN_PROGRESS if body.assignee_id else TaskStatus.TODO,
    )
    return await store.add_task(task)


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, body: UpdateTask,
                      store: Store = Depends(get_store)):
    """사람이 상태를 바꾼다. 에이전트와 달리 done 으로도 옮길 수 있다."""
    updated = await store.update_task(task_id, status=body.status)
    if updated is None:
        raise HTTPException(404, "태스크가 없습니다")
    return updated


@app.get("/api/channels/{channel_id}/stream")
async def stream(channel_id: str):
    """SSE. 프론트는 EventSource 로 붙어서 에이전트 발화를 실시간으로 받는다."""
    async def gen():
        yield ": connected\n\n"
        async for payload in bus.subscribe(channel_id):
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
