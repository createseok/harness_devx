"""전 계층 통합 데모 — 의존성 0, 사내 AI 없이 협업 흐름 전체를 재현한다.

    PYTHONPATH=. python3 scripts/demo.py

시나리오: #결제-이슈 채널에 사람이 요청 → 기획자가 받아 분석가에게 위임 →
분석가가 결과를 내고 개발자에게 넘김 → 개발자가 조치안을 보고.
"""
from __future__ import annotations

import asyncio
import sys

from app.core.engine import Engine
from app.core.guards import TraceBudget
from app.core.models import (
    Agent, Channel, ChannelMember, Human, MemberType, Message, MessageKind,
    ReplyMode, new_id,
)
from app.core.tools import registry
from app.llm.mock import ScriptedProvider
from app.store.memory import InMemoryStore

WS = "ws_demo"

# --- 사내 AI가 뱉을 법한 응답들. 일부러 형식을 흐트러뜨려 파서를 함께 검증한다 ---
SCRIPT = {
    "기획자": [
        # 정석 포맷 + 위임
        '```action\n{"tool":"post_message","args":{"text":"확인했습니다. 결제 실패율 급증 건, 먼저 데이터부터 보겠습니다."}}\n```\n'
        '```action\n{"tool":"mention_agent","args":{"agent":"데이터분석가","request":"최근 7일 결제 실패율을 PG사별/에러코드별로 쪼개서 어디서 튀는지 알려주세요."}}\n```\n'
        '```action\n{"tool":"finish","args":{"summary":"분석가에게 위임"}}\n```',
        # 두 번째 턴: 트레일링 콤마 + 주석 (더러운 JSON)
        '```action\n{\n  // 결과를 받았으니 정리한다\n  "tool": "post_message",\n'
        '  "args": {"text": "정리하면 — 원인은 A사 3D인증 타임아웃, 조치는 재시도 로직 추가입니다. 오늘 중 배포 목표로 진행합니다.",},\n}\n```\n'
        '```action\n{"tool":"finish","args":{"summary":"결론 정리"}}\n```',
    ],
    "데이터분석가": [
        # 도구 이름 오타 (post_msg) — 보정되어야 함
        '```action\n{"tool":"post_msg","args":{"text":"분석 결과: 최근 7일 결제 실패율 2.1%→8.7%. 증가분의 91%가 A사 PG, 에러코드 TIMEOUT_3DS 단일 원인입니다. 8/19 14시부터 급증."}}\n```\n'
        '```action\n{"tool":"mention_agent","args":{"agent":"개발자","request":"A사 PG의 3DS 인증 타임아웃(TIMEOUT_3DS) 처리 코드 확인하고 재시도 전략 제안 부탁드립니다."}}\n```\n'
        '```action\n{"tool":"finish","args":{"summary":"원인 특정 후 개발자에게 인계"}}\n```',
    ],
    "개발자": [
        # 펜스 없이 날것 JSON
        '{"tool":"post_message","args":{"text":"코드 확인했습니다. 3DS 콜백 타임아웃이 8초 고정이고 재시도가 없습니다. 조치안: (1) 타임아웃 15초 상향 (2) 지수백오프 2회 재시도 (3) 실패 시 대체 PG 폴백. (1)(2)는 오늘 배포 가능합니다."}}\n'
        '{"tool":"finish","args":{"summary":"조치안 제시"}}',
    ],
}


def seed(store: InMemoryStore):
    ch = store.put_channel(Channel(id="ch_pay", workspace_id=WS, name="결제-이슈",
                                   topic="결제 실패율 급증 대응"))
    human = store.put_human(Human(id="h_seok", workspace_id=WS, name="석"))
    store.join(ChannelMember(ch.id, MemberType.HUMAN, human.id))

    agents = [
        Agent(id="agt_pm", workspace_id=WS, name="기획자",
              role_prompt="너는 결제팀 PM이다. 요청을 받으면 스스로 다 하려 하지 말고 "
                          "적임자에게 위임하고, 결과를 종합해 사람에게 보고한다."),
        Agent(id="agt_da", workspace_id=WS, name="데이터분석가",
              role_prompt="너는 데이터 분석가다. 지표를 쪼개서 원인을 특정하고 "
                          "숫자로 말한다. 추측은 추측이라고 명시한다."),
        Agent(id="agt_dev", workspace_id=WS, name="개발자",
              role_prompt="너는 백엔드 개발자다. 코드 레벨 원인과 구체적인 조치안을 "
                          "우선순위와 함께 제시한다."),
    ]
    for a in agents:
        store.put_agent(a)
        store.join(ChannelMember(ch.id, MemberType.AGENT, a.id))
    return ch, human


def printer(m: Message):
    icon = "🧑" if m.author_type == MemberType.HUMAN else "🤖"
    indent = "   " * m.depth
    print(f"{indent}{icon} {m.author_name}: {m.text}")
    print()


async def main() -> int:
    store = InMemoryStore()
    ch, human = seed(store)
    provider = ScriptedProvider(SCRIPT)

    engine = Engine(
        store, provider, registry,
        default_budget=TraceBudget(trace_id="", max_depth=4, max_runs=20),
        on_message=printer,
    )
    await engine.start()

    print("=" * 72)
    print(f"#{ch.name} — {ch.topic}")
    print("=" * 72 + "\n")

    await engine.submit(Message(
        id=new_id("msg"), channel_id=ch.id,
        author_type=MemberType.HUMAN, author_id=human.id, author_name=human.name,
        text="@기획자 어제부터 결제 실패율이 확 올랐다는데 원인 파악하고 조치안까지 정리해주세요.",
    ))

    await engine.wait_idle()
    await engine.stop()

    print("=" * 72)
    s = engine.stats
    print(f"에이전트 턴: {s.turns}회   메시지: {s.messages}개   "
          f"토큰(mock): {s.tokens:,}")
    if s.skipped:
        print(f"깨우지 않음: {len(s.skipped)}건")
        for x in s.skipped:
            print(f"  - {x}")

    # 검증
    ok = True
    transcript = await store.recent_messages(ch.id, limit=100)
    speakers = [m.author_name for m in transcript]
    for expected in ["기획자", "데이터분석가", "개발자"]:
        if expected not in speakers:
            print(f"✗ {expected} 가 발언하지 않았습니다")
            ok = False
    depths = {m.author_name: m.depth for m in transcript}
    print(f"\n홉 깊이: {depths}")
    print("툴 로그(관측용, UI에서는 접힘):",
          len([m for m in store.messages.values() if m.kind == MessageKind.TOOL_LOG]), "건")
    print("\n" + ("✓ 위임 체인 정상 동작" if ok else "✗ 체인 끊김"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
