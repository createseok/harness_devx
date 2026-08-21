"""롤링 요약 검증.

처음엔 "채널이 길어지면 컨텍스트가 터진다"를 잡으려 했는데, 재보니 그건
사실이 아니었다. MAX_HISTORY 가 이미 크기를 묶고 있었다 (400건 채널에서도
컨텍스트는 1,811자로 일정).

진짜 문제는 **윈도 밖 정보가 통째로 사라지는 것**이다. 100건이 넘으면
초기에 확정된 결정이 프롬프트에 흔적도 남지 않아, 나중에 합류한 에이전트가
과거를 전혀 모른 채 일한다.

그래서 이 테스트가 재는 것:
  1. 같은 컨텍스트 예산으로 채널 전체를 커버하는가 (정보 보존)
  2. 채널이 길어져도 컨텍스트가 유계인가 (회귀 방지)
  3. 요약 1회 비용이 채널 길이와 무관한가 (증분)
"""
from __future__ import annotations

import asyncio
import sys

from app.core.context import MAX_HISTORY, build_context_block
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, new_id,
)
from app.core.summarizer import RECENT_WINDOW, Summarizer
from app.llm.base import ChatMessage, LLMProvider, LLMResponse, Usage
from app.store.memory import InMemoryStore

WS = "ws_mem"
DECISION = "PG사 A 의 3DS 타임아웃을 8초에서 15초로 올리기로 확정"


def check(label, actual, expected):
    good = actual == expected
    print(f"[{'PASS' if good else 'FAIL'}] {label}" +
          ("" if good else f"  기대={expected!r} 실제={actual!r}"))
    return 0 if good else 1


def ok(label, cond, note=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({note})" if note else ""))
    return 0 if cond else 1


class RecordingLLM(LLMProvider):
    """요약 호출을 세고 입력 크기를 기록한다. 초기 결정을 요약에 실어 보낸다."""

    supports_native_tools = False
    default_model = "fake"

    def __init__(self):
        self.calls = 0
        self.input_sizes = []
        self.saw_previous = []

    async def chat(self, messages, tools=None, *, model=None, temperature=0.3,
                   max_tokens=2048):
        self.calls += 1
        body = "\n".join(m.content for m in messages)
        self.input_sizes.append(len(body))
        self.saw_previous.append("# 지금까지의 요약" in body)
        # 실제 요약기처럼, 입력에서 본 결정 사항을 요약에 담는다
        carried = DECISION if (DECISION in body) else "초기 결정 없음"
        return LLMResponse(text=f"[{self.calls}세대] {carried}. 원인 미확정, 데이터 접근이 블로커.",
                           usage=Usage(len(body) // 4, 40))


async def seed(store, n: int, *, decision_at: int = 2):
    ch = store.put_channel(Channel(id="c1", workspace_id=WS, name="긴채널"))
    store.put_human(Human(id="h1", workspace_id=WS, name="석"))
    store.join(ChannelMember(ch.id, MemberType.HUMAN, "h1"))
    a = store.put_agent(Agent(id="a1", workspace_id=WS, name="분석가",
                              role_prompt="분석가다"))
    store.join(ChannelMember(ch.id, MemberType.AGENT, a.id))
    for i in range(n):
        text = (DECISION if i == decision_at
                else f"[{i:03d}] 결제 관련 논의 " + "상세 설명 " * 12)
        await store.add_message(Message(
            id=new_id("msg"), channel_id=ch.id,
            author_type=MemberType.HUMAN if i % 3 == 0 else MemberType.AGENT,
            author_id="h1" if i % 3 == 0 else "a1",
            author_name="석" if i % 3 == 0 else "분석가",
            text=text, trace_id=f"tr{i}"))
    return ch, a


async def context_of(store, agent, ch):
    trigger = (await store.recent_messages(ch.id, limit=1))[0]
    return await build_context_block(store, agent, ch, trigger)


async def run() -> int:
    f = 0

    # ── 1) 기준선: 요약 없이 긴 채널이면 초기 결정이 사라진다
    bare = InMemoryStore()
    ch, agent = await seed(bare, 300)
    before = await context_of(bare, agent, ch)
    print(f"\n요약 없음  → 컨텍스트 {len(before):,}자 "
          f"(최근 {MAX_HISTORY}건만)")
    f += ok("요약 없으면 초기 결정이 컨텍스트에서 사라진다",
            DECISION not in before, "← 이게 해결하려는 문제")

    # ── 2) 요약을 켜면 같은 예산으로 초기 결정이 살아난다
    store = InMemoryStore()
    ch, agent = await seed(store, 300)
    llm = RecordingLLM()
    summ = Summarizer(store, llm)

    f += ok("300건이면 요약이 필요하다고 판단", await summ.needs_update(ch.id))
    s1 = await summ.maybe_update(ch.id)
    f += ok("요약 생성됨", s1 is not None)
    f += check("첫 호출은 이전 요약 없이 시작", llm.saw_previous[0], False)
    f += ok("백로그를 따라잡았다 (배치 상한을 넘겨 여러 번 돌았다)",
            not await summ.needs_update(ch.id), f"LLM {llm.calls}회")
    f += check("최근 N개를 제외한 전부를 커버", s1.covered_count, 300 - RECENT_WINDOW)

    after = await context_of(store, agent, ch)
    print(f"요약 적용  → 컨텍스트 {len(after):,}자 "
          f"(요약 {s1.covered_count}건 + 원문 {RECENT_WINDOW}건)")
    f += ok("★ 초기 결정이 요약을 통해 되살아남", DECISION in after,
            "← 이 단계의 목적")
    f += ok("요약 블록 존재", "## 이전 대화 요약" in after)
    f += ok("최근 대화 원문도 존재", "## 최근 대화" in after)
    f += ok("압축 건수를 밝힌다", f"{s1.covered_count}건 압축" in after)
    f += ok("컨텍스트가 예산 안에 있다", len(after) < len(before) * 1.5,
            f"{len(before):,} → {len(after):,}")

    # ── 3) 갱신 직후에는 다시 요약하지 않는다
    calls_before = llm.calls
    f += check("불필요할 때는 호출 안 함", await summ.maybe_update(ch.id), None)
    f += check("LLM 호출 증가 없음", llm.calls, calls_before)

    # ── 4) ★ 증분인가 — 따라잡은 뒤의 추가분은 훨씬 싸야 한다
    for i in range(300, 340):
        await store.add_message(Message(
            id=new_id("msg"), channel_id=ch.id, author_type=MemberType.AGENT,
            author_id="a1", author_name="분석가",
            text=f"[{i:03d}] 추가 논의 " + "상세 설명 " * 12, trace_id="tr"))
    idx = llm.calls
    s2 = await summ.maybe_update(ch.id)
    f += ok("추가 40건 후 재요약", s2 is not None)
    f += check("이번엔 이전 요약을 입력으로 받음", llm.saw_previous[idx], True)
    first, inc = llm.input_sizes[0], llm.input_sizes[idx]
    print(f"요약 입력  → 최초 {first:,}자 · 증분 {inc:,}자")
    f += ok("★ 증분 비용이 최초의 절반 미만", inc < first * 0.5,
            f"{first:,} → {inc:,}")
    f += ok("누적 커버가 늘어남", s2.covered_count > s1.covered_count,
            f"{s1.covered_count} → {s2.covered_count}")

    # ── 5) 채널이 계속 자라도 컨텍스트가 유계인가 + 결정이 계속 보존되는가
    sizes, kept = [], []
    for batch in range(3):
        for i in range(80):
            await store.add_message(Message(
                id=new_id("msg"), channel_id=ch.id, author_type=MemberType.AGENT,
                author_id="a1", author_name="분석가",
                text=f"[b{batch}-{i:03d}] 논의 " + "상세 설명 " * 12, trace_id="tr"))
        await summ.maybe_update(ch.id)
        blk = await context_of(store, agent, ch)
        sizes.append(len(blk))
        kept.append(DECISION in blk)
    total = await store.count_messages_after(ch.id, None)
    print(f"채널 {total}건까지 성장 → 컨텍스트 {sizes}")
    f += ok("컨텍스트가 발산하지 않는다",
            max(sizes) < 6000, f"최대 {max(sizes):,}자")
    f += ok("성장해도 편차가 작다",
            (max(sizes) - min(sizes)) < max(sizes) * 0.35,
            f"편차 {max(sizes) - min(sizes):,}자")
    f += check("★ 성장 내내 초기 결정이 보존됨", kept, [True, True, True])

    # ── 6) 요약 실패가 에이전트를 막으면 안 된다
    class Broken(LLMProvider):
        supports_native_tools = False
        default_model = "broken"

        async def chat(self, *a, **k):
            raise RuntimeError("사내 AI 다운")

    bstore = InMemoryStore()
    bch, bagent = await seed(bstore, 200)
    bs = Summarizer(bstore, Broken())
    f += check("요약 실패 시 None (예외 전파 안 함)", await bs.maybe_update(bch.id), None)
    blk = await context_of(bstore, bagent, bch)
    f += ok("요약이 없어도 컨텍스트는 정상 생성", "## 최근 대화" in blk)
    return f


if __name__ == "__main__":
    n = asyncio.run(run())
    print("-" * 60)
    print("모두 통과" if n == 0 else f"{n}건 실패")
    sys.exit(1 if n else 0)
