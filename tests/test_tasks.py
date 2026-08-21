"""태스크 보드 검증.

두 저장소 구현(인메모리/Postgres) 모두에 같은 시나리오를 돌린다.
DATABASE_URL 이 있으면 SqlStore 도 함께 검증한다.
"""
from __future__ import annotations

import asyncio
import os
import sys

from app.core.guards import TraceBudget
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, MessageKind,
    Task, TaskStatus, new_id,
)
from app.core.tools import ToolContext, registry
from app.store.memory import InMemoryStore

WS = "ws_task"


def check(label, actual, expected):
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  기대={expected!r} 실제={actual!r}"))
    return 0 if ok else 1


def ok(label, cond):
    return check(label, bool(cond), True)


async def build_memory():
    s = InMemoryStore()
    ch = s.put_channel(Channel(id="c1", workspace_id=WS, name="결제-이슈"))
    s.put_human(Human(id="h1", workspace_id=WS, name="석"))
    s.join(ChannelMember(ch.id, MemberType.HUMAN, "h1"))
    for name in ("기획자", "개발자", "분석가"):
        a = s.put_agent(Agent(id=f"agt_{name}", workspace_id=WS, name=name,
                              role_prompt=f"{name}다"))
        s.join(ChannelMember(ch.id, MemberType.AGENT, a.id))
    return s, ch


def ctx_for(store, agent_id, channel_id):
    trigger = Message(id=new_id("msg"), channel_id=channel_id,
                      author_type=MemberType.HUMAN, author_id="h1",
                      author_name="석", text="요청", trace_id="tr")
    return ToolContext(store=store, agent=store.agents[agent_id],
                       channel_id=channel_id, trigger_message=trigger,
                       budget=TraceBudget("tr"))


async def scenario(store, channel_id, label: str) -> int:
    f = 0
    print(f"\n── {label}")
    pm = ctx_for(store, "agt_기획자", channel_id)
    dev = ctx_for(store, "agt_개발자", channel_id)
    ana = ctx_for(store, "agt_분석가", channel_id)

    # 1) 담당자 지정 생성 → 멘션 메시지가 나가야 한다
    r = await registry.execute("create_task", {
        "title": "3DS 타임아웃 재시도 로직 추가", "assignee": "개발자"}, pm)
    f += ok("담당자 지정 생성", "태스크 생성" in r and "@개발자" in r)
    assigned = [t for t in await store.list_tasks(channel_id)][0]
    f += check("담당자 지정 시 바로 진행 중", assigned.status, TaskStatus.IN_PROGRESS)
    f += ok("배정 메시지가 담당자를 멘션", any("@개발자" in m.text for m in pm.emitted))
    f += ok("태스크 스레드가 열림", assigned.thread_id is not None)

    # 2) 없는 에이전트 지정 → 명확한 오류
    r = await registry.execute("create_task", {"title": "x", "assignee": "없는사람"}, pm)
    f += ok("없는 담당자는 오류", r.startswith("오류"))

    # 3) 미배정 생성 → claim 경합
    await registry.execute("create_task", {"title": "PG 응답코드 매핑표 정리"}, pm)
    free = [t for t in await store.list_tasks(channel_id) if t.assignee_id is None][0]
    f += check("미배정 태스크는 todo", free.status, TaskStatus.TODO)

    # ★ 두 에이전트가 동시에 같은 태스크를 집는다 — 정확히 1명만 성공해야 한다
    results = await asyncio.gather(
        registry.execute("claim_task", {"task_id": free.id}, dev),
        registry.execute("claim_task", {"task_id": free.id}, ana),
    )
    wins = sum(1 for r in results if "맡았습니다" in r)
    f += check("동시 claim 중 정확히 1명만 성공", wins, 1)
    f += ok("실패한 쪽은 중복하지 말라고 안내",
            any("선점 실패" in r and "중복" in r for r in results))

    claimed = await store.get_task(free.id)
    f += check("claim 후 진행 중", claimed.status, TaskStatus.IN_PROGRESS)

    # 4) 남의 태스크는 못 옮긴다
    other = ana if claimed.assignee_id == "agt_개발자" else dev
    r = await registry.execute("update_task_status",
                               {"task_id": free.id, "status": "in_review"}, other)
    f += ok("남의 태스크 상태 변경 차단", r.startswith("오류") and "담당자" in r)

    # 5) 담당자는 옮길 수 있다 + 상태값 표기 흔들림 허용
    owner = dev if claimed.assignee_id == "agt_개발자" else ana
    r = await registry.execute("update_task_status",
                               {"task_id": free.id, "status": "in-review",
                                "note": "1차 정리 완료"}, owner)
    f += ok("담당자는 변경 가능 ('in-review' 표기도 허용)", "in_review" in r)
    f += check("상태 반영됨", (await store.get_task(free.id)).status, TaskStatus.IN_REVIEW)

    # 6) done 은 사람 승인 영역
    r = await registry.execute("update_task_status",
                               {"task_id": free.id, "status": "done"}, owner)
    f += ok("에이전트는 done 으로 못 옮김", r.startswith("오류") and "in_review" in r)

    # 7) 알 수 없는 상태
    r = await registry.execute("update_task_status",
                               {"task_id": free.id, "status": "완료함"}, owner)
    f += ok("알 수 없는 상태는 가능한 값 안내", r.startswith("오류") and "todo" in r)

    # 8) 없는 태스크
    r = await registry.execute("claim_task", {"task_id": "tsk_없음"}, dev)
    f += ok("없는 태스크는 오류", r.startswith("오류"))

    # 9) 보드 조회
    board = await registry.execute("list_tasks", {}, pm)
    f += ok("보드에 상태·담당자 표시", "진행 중" in board and "@개발자" in board)
    f += ok("보드에 검토 요청 표시", "검토 요청" in board)

    # 10) 진행 보고는 태스크 스레드로
    before = len(dev.emitted)
    await registry.execute("post_task_update",
                           {"task_id": assigned.id, "text": "코드 확인 중입니다"}, dev)
    f += check("진행 보고 1건 발생", len(dev.emitted) - before, 1)
    f += check("태스크 스레드에 달림", dev.emitted[-1].thread_id, assigned.thread_id)

    # 11) 컨텍스트에 보드가 주입되는가
    from app.core.context import build_context_block
    ch = await store.get_channel(channel_id)
    block = await build_context_block(store, store.agents["agt_개발자"], ch,
                                      dev.trigger_message)
    f += ok("컨텍스트에 태스크 보드 포함", "## 태스크 보드" in block)
    f += ok("내 담당이 표시됨", "**내 담당**" in block)
    return f


async def run() -> int:
    f = 0
    store, ch = await build_memory()
    f += await scenario(store, ch.id, "InMemoryStore")

    url = os.getenv("DATABASE_URL")
    if url:
        from sqlalchemy import text
        from app.store.sql import AgentRow, ChannelRow, HumanRow, MemberRow, SqlStore
        sql = SqlStore(url)
        await sql.create_all()
        async with sql.engine.begin() as conn:
            for t in ("agent_runs", "messages", "channel_members", "tasks",
                      "agents", "humans", "channels"):
                await conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
        async with sql.session() as s, s.begin():
            s.add(ChannelRow(id="c1", workspace_id=WS, name="결제-이슈"))
            s.add(HumanRow(id="h1", workspace_id=WS, name="석"))
            s.add(MemberRow(channel_id="c1", member_type="human", member_id="h1"))
            for name in ("기획자", "개발자", "분석가"):
                s.add(AgentRow(id=f"agt_{name}", workspace_id=WS, name=name,
                               role_prompt=f"{name}다", reply_mode="mention"))
                s.add(MemberRow(channel_id="c1", member_type="agent",
                                member_id=f"agt_{name}"))
        # ctx_for 가 store.agents 를 쓰므로 캐시를 붙여준다
        sql.agents = {f"agt_{n}": await sql.get_agent(f"agt_{n}")
                      for n in ("기획자", "개발자", "분석가")}
        f += await scenario(sql, "c1", "SqlStore (Postgres)")
        await sql.engine.dispose()
    else:
        print("\n(DATABASE_URL 없음 — SqlStore 시나리오 건너뜀)")
    return f


if __name__ == "__main__":
    n = asyncio.run(run())
    print("-" * 60)
    print("모두 통과" if n == 0 else f"{n}건 실패")
    sys.exit(1 if n else 0)
