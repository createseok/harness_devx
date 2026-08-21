"""Anthropic Messages API provider.

`claude -p` 대비 장점:
  - 네이티브 tool calling (ReAct 파싱 없이 구조화된 tool_use 블록을 받는다)
  - 프로세스 스폰이 없어 훨씬 빠르고 동시성에 유리
  - temperature/effort/thinking 등 파라미터 제어 가능

단점: API 키(또는 `ant auth login` 프로필)와 별도 과금이 필요하다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.llm.base import (
    ChatMessage, LLMError, LLMProvider, LLMResponse, LLMTransientError,
    ToolCall, ToolSpec, Usage,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider(LLMProvider):
    supports_native_tools = True

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
        effort: str = "medium",
        max_retries: int = 3,
        timeout: float = 120.0,
    ) -> None:
        self.default_model = default_model
        self.effort = effort
        self._api_key = api_key
        self._max_retries = max_retries
        self._timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMError("anthropic SDK가 필요합니다: pip install anthropic") from exc
            # api_key 를 넘기지 않으면 SDK가 ANTHROPIC_API_KEY →
            # ANTHROPIC_AUTH_TOKEN → `ant auth login` 프로필 순으로 알아서 찾는다.
            kwargs: Dict[str, Any] = {
                "max_retries": self._max_retries,
                "timeout": self._timeout,
            }
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    @staticmethod
    def _split(messages: List[ChatMessage]) -> tuple:
        """ChatMessage 목록을 (system 문자열, Anthropic messages 배열) 로 변환한다.

        연속된 tool 메시지는 하나의 user 턴으로 묶는다 — Anthropic API는
        여러 tool_result 를 한 user 메시지에 담아야 한다.
        """
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []
        pending_results: List[Dict[str, Any]] = []

        def flush_results():
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
                continue

            if m.role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "unknown",
                    "content": m.content,
                })
                continue

            flush_results()
            if m.role == "assistant":
                # 네이티브 블록이 있으면 그대로 왕복시킨다 (tool_use 짝 유지)
                content = m.blocks if m.blocks else (m.content or "")
                if content:
                    out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": m.content})

        flush_results()
        return "\n\n".join(system_parts), out

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolSpec]] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        import anthropic

        client = self._get_client()
        system, msgs = self._split(messages)

        kwargs: Dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "messages": msgs,
            # 적응형 사고 — 위임 판단처럼 다단계 추론이 필요한 턴에서 품질이 오른다
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]

        try:
            resp = await client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMTransientError(f"레이트리밋: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransientError(f"네트워크 오류: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMTransientError(f"서버 오류 {exc.status_code}: {exc}") from exc
            raise LLMError(f"API 오류 {exc.status_code}: {exc}") from exc

        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            raise LLMError(f"안전 정책으로 거절됨: {getattr(detail, 'category', None)}")

        text_parts: List[str] = []
        calls: List[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name,
                                      arguments=dict(block.input or {})))

        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
            # runtime 이 assistant 턴을 왕복시킬 때 원본 블록을 쓴다
            raw={"content": resp.content, "stop_reason": resp.stop_reason},
            finish_reason=resp.stop_reason or "stop",
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
