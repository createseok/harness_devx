"""claude -p 연결 확인.

    PYTHONPATH=. python3 scripts/probe_claude_cli.py

probe_corp.py 와 같은 역할이다. 어댑터가 실제로 도는지,
그리고 ReAct 형식을 지킬 수 있는지 확인한다.
"""
from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.core.react import FORMAT_INSTRUCTIONS, parse_actions
from app.llm.base import ChatMessage, ToolSpec
from app.llm.claude_cli import ClaudeCliProvider

TOOL = ToolSpec(
    "post_message", "채널에 메시지를 올린다.",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)


async def main() -> int:
    p = ClaudeCliProvider(
        cli_path=settings.claude_cli_path,
        default_model=settings.claude_cli_model,
        timeout=settings.claude_cli_timeout,
    )
    try:
        print(f"CLI 경로: {p._resolve_cli()}")
    except Exception as exc:
        print(f"✗ {exc}")
        return 1
    print(f"모델: {settings.claude_cli_model}\n")

    print("[1/2] 기본 호출 …")
    try:
        r = await p.chat([ChatMessage("user", "'연결 성공'이라고만 답하세요.")])
        print(f"  ✓ 응답: {r.text[:120]!r}")
        print(f"  ✓ 토큰: in={r.usage.prompt_tokens} out={r.usage.completion_tokens}"
              f"  비용: ${p.total_cost_usd:.4f}")
    except Exception as exc:
        print(f"  ✗ 실패: {exc}")
        return 1

    print("\n[2/2] ReAct 형식 준수력 (3회) …")
    system = ("너는 채널 에이전트다.\n\n# 사용 가능한 도구\n"
              + TOOL.to_prompt_block() + "\n\n" + FORMAT_INSTRUCTIONS)
    ok = 0
    for i in range(3):
        try:
            r = await p.chat([ChatMessage("system", system),
                              ChatMessage("user", "채널에 '안녕하세요'라고 인사해주세요.")])
            calls, _, warns = parse_actions(r.text, known_tools=["post_message", "finish"])
            hit = any(c.name == "post_message" for c in calls)
            clean = hit and not any("action 블록이 없어" in w for w in warns)
            ok += 1 if clean else 0
            print(f"  {'✓' if clean else ('~' if hit else '✗')} 시도 {i+1}: "
                  f"{[c.name for c in calls]}" + (f"  ({warns[0]})" if warns else ""))
        except Exception as exc:
            print(f"  ✗ 시도 {i+1}: {exc}")

    print(f"\n형식 준수 {ok}/3   총 비용 ${p.total_cost_usd:.4f}")
    print("→ 준비 완료. .env 에 LLM_PROVIDER=claude_cli 로 두고 개발하세요."
          if ok >= 2 else "→ role_prompt 에 형식 예시를 추가하는 것이 좋습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
