"""테스트/데모용 provider.

ScriptedProvider 는 에이전트 이름별로 미리 정해둔 원문을 순서대로 뱉는다.
일부러 **형식이 어긋난 응답**을 섞어두어, 실제 루프 안에서도 ReAct 파서가
버티는지 함께 검증한다.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from app.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolSpec, Usage

_AGENT_NAME = re.compile(r"내 이름: @([^\s(]+)")


class ScriptedProvider(LLMProvider):
    supports_native_tools = False
    default_model = "mock"

    def __init__(self, script: Dict[str, List[str]], fallback: Optional[str] = None) -> None:
        self.script = script
        self.fallback = fallback or (
            '```action\n{"tool":"post_message","args":{"text":"확인했습니다."}}\n```\n'
            '```action\n{"tool":"finish","args":{"summary":"응답 완료"}}\n```'
        )
        self.calls: List[Dict[str, str]] = []
        self._cursor: Dict[str, int] = {}

    def _whoami(self, messages: List[ChatMessage]) -> str:
        for m in messages:
            if m.role == "system":
                found = _AGENT_NAME.search(m.content)
                if found:
                    return found.group(1)
        return "unknown"

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolSpec]] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        name = self._whoami(messages)
        idx = self._cursor.get(name, 0)
        lines = self.script.get(name, [])
        text = lines[idx] if idx < len(lines) else self.fallback
        self._cursor[name] = idx + 1

        self.calls.append({
            "agent": name,
            "step": str(idx),
            "tools": ",".join(t.name for t in (tools or [])),
        })
        prompt_chars = sum(len(m.content) for m in messages)
        return LLMResponse(
            text=text,
            usage=Usage(prompt_chars // 4, len(text) // 4),
            raw={"mock": True},
        )


class EchoProvider(LLMProvider):
    """항상 같은 응답. 가드/폭주 테스트용."""

    supports_native_tools = False
    default_model = "echo"

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = 0

    async def chat(self, messages, tools=None, *, model=None, temperature=0.3, max_tokens=2048):
        self.n += 1
        return LLMResponse(text=self.text, usage=Usage(100, 20), raw={"echo": True})
