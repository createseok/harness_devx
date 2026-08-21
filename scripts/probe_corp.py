"""사내 AI 연결 확인 — 0단계에서 가장 먼저 돌려볼 스크립트.

    PYTHONPATH=. python3 scripts/probe_corp.py

확인하는 것:
  1. 인증/엔드포인트가 맞는가              → corp.py [EDIT 1]
  2. 요청 바디를 받아주는가                → corp.py [EDIT 2]
  3. 응답을 파싱할 수 있는가                → corp.py [EDIT 3]
  4. 네이티브 tool calling을 지원하는가     → CORP_AI_NATIVE_TOOLS 결정
  5. ReAct 폴백으로 형식을 지킬 수 있는가   → 실제 에이전트 품질의 하한선
"""
from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.core.react import FORMAT_INSTRUCTIONS, parse_actions
from app.llm.base import ChatMessage, ToolSpec
from app.llm.corp import CorpProvider

TOOL = ToolSpec(
    "post_message",
    "채널에 메시지를 올린다.",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)


async def main() -> int:
    try:
        settings.validate()
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1

    p = CorpProvider(
        base_url=settings.corp_ai_base_url,
        api_key=settings.corp_ai_api_key,
        default_model=settings.corp_ai_model,
        supports_native_tools=settings.corp_ai_native_tools,
        timeout=settings.corp_ai_timeout,
    )
    print(f"엔드포인트: {settings.corp_ai_base_url}")
    print(f"모델: {settings.corp_ai_model}\n")

    # 1~3단계: 단순 호출
    print("[1/3] 기본 호출 …")
    try:
        r = await p.chat([ChatMessage("user", "'연결 성공'이라고만 답하세요.")])
        print(f"  ✓ 응답: {r.text[:120]!r}")
        print(f"  ✓ 토큰: prompt={r.usage.prompt_tokens} completion={r.usage.completion_tokens}")
        if r.usage.total == 0:
            print("  ⚠️  토큰 정보가 0입니다 — corp.py [EDIT 3]에서 usage 필드를 매핑하세요.")
    except Exception as exc:
        print(f"  ✗ 실패: {exc}")
        print("  → corp.py 의 [EDIT 1](인증) / [EDIT 2](요청) / 엔드포인트 경로를 확인하세요.")
        await p.aclose()
        return 1

    # 4단계: 네이티브 tool calling
    print("\n[2/3] 네이티브 tool calling …")
    p.supports_native_tools = True
    try:
        r = await p.chat(
            [ChatMessage("user", "채널에 '안녕하세요'라고 올려주세요.")], [TOOL]
        )
        if r.tool_calls:
            print(f"  ✓ 지원함 → {r.tool_calls[0].name}({r.tool_calls[0].arguments})")
            print("  → .env 에 CORP_AI_NATIVE_TOOLS=true 로 설정하세요.")
        else:
            print("  – tool_calls 가 비어 있습니다 (미지원으로 보임)")
            print("  → CORP_AI_NATIVE_TOOLS=false 로 두면 ReAct 폴백이 동작합니다.")
    except Exception as exc:
        print(f"  – 미지원: {str(exc)[:150]}")
        print("  → CORP_AI_NATIVE_TOOLS=false 로 두세요.")
    finally:
        p.supports_native_tools = settings.corp_ai_native_tools

    # 5단계: ReAct 형식 준수력 — 여기가 실제 에이전트 품질의 하한선
    print("\n[3/3] ReAct 형식 준수력 (3회 시도) …")
    system = (
        "너는 채널 에이전트다.\n\n# 사용 가능한 도구\n"
        + TOOL.to_prompt_block() + "\n\n" + FORMAT_INSTRUCTIONS
    )
    ok = 0
    for i in range(3):
        try:
            r = await p.chat([
                ChatMessage("system", system),
                ChatMessage("user", "채널에 '안녕하세요'라고 인사해주세요."),
            ])
            calls, _, warns = parse_actions(r.text, known_tools=["post_message", "finish"])
            hit = any(c.name == "post_message" for c in calls)
            clean = hit and not any("action 블록이 없어" in w for w in warns)
            ok += 1 if clean else 0
            mark = "✓" if clean else ("~" if hit else "✗")
            print(f"  {mark} 시도 {i+1}: {[c.name for c in calls]}"
                  + (f"  ({warns[0]})" if warns else ""))
        except Exception as exc:
            print(f"  ✗ 시도 {i+1}: {exc}")

    await p.aclose()
    print(f"\n형식 준수 {ok}/3")
    if ok == 3:
        print("→ 그대로 진행하세요.")
    elif ok >= 1:
        print("→ 동작은 합니다(관대 파서가 흡수). role_prompt에 형식 예시를 1~2개 넣으면 좋아집니다.")
    else:
        print("→ 형식을 못 지킵니다. temperature를 낮추고, 시스템 프롬프트에 "
              "few-shot 예시를 넣거나 더 큰 모델로 라우팅하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
