"""SqlStore 실동작 검증 (Postgres 필요).

    TEST_DATABASE_URL=postgresql+asyncpg://genteam:genteam@127.0.0.1/genteam_test \
      PYTHONPATH=. .venv/bin/python tests/test_sql_store.py

인메모리 구현과 같은 계약을 지키는지, 특히 claim_run 의 원자성을 본다.
멱등성은 워커를 여러 프로세스로 늘렸을 때 중복 실행을 막는 핵심 장치라
DB 유니크 제약이 실제로 걸리는지 반드시 확인해야 한다.
"""
from __future__ import annotations

import asyncio
import os
import sys

from app.core.models import (
    Agent, AgentRun, Channel, ChannelMember, Human, MemberType, Message,
    MessageKind, ReplyMode, RunStatus, Task, TaskStatus, new_id,
)
from app.store.sql import SqlStore

from tests._dbutil import DEFAULT, fresh_store, resolve_test_url

URL = resolve_test_url() or DEFAULT
WS = "ws_sqltest"


def check(label, actual, expected):
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  기대={expected} 실제={actual}"))
    return 0 if ok else 1


async def run() -> int:
    f = 0
    store = await fresh_store(URL)   # 테스트 DB 인지 확인 후 비운다

    from app.store.sql import AgentRow, ChannelRow, HumanRow, MemberRow
    ch_id, h_id, a_id, b_id = new_id("ch"), new_id("usr"), new_id("agt"), new_id("agt")
    async with store.session() as s, s.begin():
        s.add(ChannelRow(id=ch_id, workspace_id=WS, name="결제-이슈", topic="테스트"))
        s.add(HumanRow(id=h_id, workspace_id=WS, name="석"))
        s.add(AgentRow(id=a_id, workspace_id=WS, name="분석가", role_prompt="분석가다",
                       reply_mode="mention"))
        s.add(AgentRow(id=b_id, workspace_id=WS, name="개발자", role_prompt="개발자다",
                       reply_mode="all"))
        s.add(MemberRow(channel_id=ch_id, member_type="human", member_id=h_id))
        s.add(MemberRow(channel_id=ch_id, member_type="agent", member_id=a_id))
        s.add(MemberRow(channel_id=ch_id, member_type="agent", member_id=b_id,
                        reply_mode="mention"))

    # --- 조회 ---
    f += check("채널 조회", (await store.get_channel(ch_id)).name, "결제-이슈")
    f += check("사람 조회", (await store.get_human(h_id)).name, "석")
    f += check("에이전트 조회", (await store.get_agent(a_id)).name, "분석가")
    f += check("핸들로 에이전트 조회(대소문자 무시)",
               (await store.get_agent_by_name(WS, "@분석가")).id, a_id)
    f += check("없는 핸들은 None", await store.get_agent_by_name(WS, "없는사람"), None)

    members = await store.channel_members(ch_id)
    f += check("멤버 3명", len(members), 3)
    override = [m for m in members if m.member_id == b_id][0]
    f += check("채널별 reply_mode 오버라이드 보존", override.reply_mode, ReplyMode.MENTION)

    # --- 메시지 (append-only) ---
    root = Message(id=new_id("msg"), channel_id=ch_id, author_type=MemberType.HUMAN,
                   author_id=h_id, author_name="석", text="결제 실패율 분석해줘",
                   trace_id="tr1")
    await store.add_message(root)
    await store.add_message(Message(
        id=new_id("msg"), channel_id=ch_id, author_type=MemberType.AGENT,
        author_id=a_id, author_name="분석가", text="내부 툴 기록",
        kind=MessageKind.TOOL_LOG, trace_id="tr1", depth=1))
    reply = Message(id=new_id("msg"), channel_id=ch_id, author_type=MemberType.AGENT,
                    author_id=a_id, author_name="분석가", text="확인했습니다",
                    thread_id=root.id, trace_id="tr1", depth=1, caused_by=root.id)
    await store.add_message(reply)

    recent = await store.recent_messages(ch_id, limit=50)
    f += check("tool_log 는 기본 조회에서 제외", len(recent), 2)
    f += check("시간순 정렬", recent[0].id, root.id)
    f += check("include_tool_logs=True 면 포함",
               len(await store.recent_messages(ch_id, limit=50, include_tool_logs=True)), 3)
    f += check("enum 왕복 (MemberType)", recent[0].author_type, MemberType.HUMAN)
    f += check("계보 정보 보존 (caused_by)", recent[1].caused_by, root.id)
    f += check("스레드 조회 (루트 포함)", len(await store.thread_messages(root.id)), 2)
    f += check("검색", [m.text for m in await store.search_messages(ch_id, "실패율")],
               ["결제 실패율 분석해줘"])
    f += check("검색이 tool_log 는 건너뜀",
               len(await store.search_messages(ch_id, "내부 툴")), 0)

    # --- ★ 멱등성: 여기가 핵심 ---
    def mk_run():
        return AgentRun(id=new_id("run"), agent_id=a_id, channel_id=ch_id,
                        trigger_message_id=root.id, trace_id="tr1", depth=0)

    r1 = mk_run()
    f += check("claim_run 1회차 성공", await store.claim_run(r1), True)
    f += check("같은 (agent, trigger) 2회차 차단", await store.claim_run(mk_run()), False)

    # 동시 선점 — 여러 워커 프로세스를 흉내낸다. 정확히 1개만 이겨야 한다.
    results = await asyncio.gather(*[store.claim_run(mk_run()) for _ in range(8)])
    f += check("동시 8건 중 성공 0건 (이미 선점됨)", sum(results), 0)

    other = AgentRun(id=new_id("run"), agent_id=b_id, channel_id=ch_id,
                     trigger_message_id=root.id, trace_id="tr1", depth=0)
    f += check("다른 에이전트는 같은 메시지로 선점 가능", await store.claim_run(other), True)

    # 새 트리거에 대해 동시 선점 — 정확히 1건만 이겨야 한다
    m2 = Message(id=new_id("msg"), channel_id=ch_id, author_type=MemberType.HUMAN,
                 author_id=h_id, author_name="석", text="두 번째 요청", trace_id="tr2")
    await store.add_message(m2)
    race = await asyncio.gather(*[
        store.claim_run(AgentRun(id=new_id("run"), agent_id=a_id, channel_id=ch_id,
                                 trigger_message_id=m2.id, trace_id="tr2", depth=0))
        for _ in range(8)])
    f += check("경합 8건 중 정확히 1건만 성공", sum(race), 1)

    # --- 실행 기록 갱신 + 토큰 집계 ---
    r1.status = RunStatus.DONE
    r1.steps = 3
    r1.prompt_tokens, r1.completion_tokens = 1200, 340
    await store.finish_run(r1)
    other.status = RunStatus.DONE
    other.prompt_tokens, other.completion_tokens = 800, 160
    await store.finish_run(other)
    f += check("trace 토큰 집계 (1200+340+800+160)",
               await store.trace_usage("tr1"), 2500)
    f += check("다른 trace 는 섞이지 않음", await store.trace_usage("tr9"), 0)

    # --- 태스크 (Phase 3 준비) ---
    await store.add_task(Task(id=new_id("tsk"), channel_id=ch_id, title="재시도 로직 추가",
                              assignee_type=MemberType.AGENT, assignee_id=b_id,
                              status=TaskStatus.IN_PROGRESS))
    tasks = await store.list_tasks(ch_id)
    f += check("태스크 저장/조회", len(tasks), 1)
    f += check("태스크 상태 enum 왕복", tasks[0].status, TaskStatus.IN_PROGRESS)
    f += check("태스크 담당자 타입 왕복", tasks[0].assignee_type, MemberType.AGENT)

    await store.engine.dispose()
    return f


if __name__ == "__main__":
    n = asyncio.run(run())
    print("-" * 60)
    print("모두 통과" if n == 0 else f"{n}건 실패")
    sys.exit(1 if n else 0)
