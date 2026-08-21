"""★ 사내 AI 어댑터 ★

이 파일이 사내 AI 스펙에 종속되는 **유일한** 파일이다.
사내 API 문서를 보고 아래 [EDIT 1] [EDIT 2] [EDIT 3] 세 곳만 고치면 된다.
나머지(재시도/백오프/타임아웃/토큰집계/ReAct 폴백)는 그대로 두면 된다.

동작 확인 순서:
    1. scripts/probe_corp.py 로 단발 호출이 되는지 확인
    2. supports_native_tools 를 실제 지원 여부에 맞게 설정
    3. 지원하지 않으면 아무것도 안 해도 됨 — 런타임이 알아서 ReAct 모드로 돈다
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from app.llm.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    LLMTransientError,
    ToolCall,
    ToolSpec,
    Usage,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class CorpProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        *,
        supports_native_tools: bool = False,
        timeout: float = 120.0,
        max_retries: int = 3,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.supports_native_tools = supports_native_tools
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra_headers = extra_headers or {}
        self._client = None  # lazy

    # ------------------------------------------------------------------
    # [EDIT 1] 인증 헤더 — 사내 게이트웨이 인증 방식에 맞춰 수정
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # 흔한 패턴들 — 사내 스펙에 맞는 것 하나만 남기고 나머지는 지운다
            "Authorization": f"Bearer {self.api_key}",
            # "X-API-Key": self.api_key,
            # "api-key": self.api_key,
        }
        headers.update(self.extra_headers)
        return headers

    # ------------------------------------------------------------------
    # [EDIT 2] 요청 바디 — 사내 스펙의 필드명/구조에 맞춰 수정
    # ------------------------------------------------------------------
    def _build_request(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolSpec]],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        # 대부분의 사내 게이트웨이는 role/content 배열을 받는다.
        # 만약 사내 스펙이 단일 prompt 문자열만 받는다면
        # `_flatten_to_prompt()` 를 대신 쓰면 된다 (아래 정의되어 있음).
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [self._msg_to_wire(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        # 네이티브 tool calling을 지원할 때만 tools를 실어보낸다.
        # 미지원이면 런타임이 이미 툴 설명을 시스템 프롬프트에 넣어놨으므로
        # 여기서는 아무것도 하지 않는 게 맞다.
        if tools and self.supports_native_tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"

        return payload

    def _msg_to_wire(self, m: ChatMessage) -> Dict[str, Any]:
        wire: Dict[str, Any] = {"role": m.role, "content": m.content}
        if m.name:
            wire["name"] = m.name
        if m.tool_call_id:
            wire["tool_call_id"] = m.tool_call_id
        return wire

    @staticmethod
    def _flatten_to_prompt(messages: List[ChatMessage]) -> str:
        """사내 스펙이 messages 배열 대신 단일 prompt만 받을 때 사용."""
        parts = []
        for m in messages:
            speaker = m.name or m.role
            parts.append(f"[{speaker}]\n{m.content}")
        parts.append("[assistant]\n")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # [EDIT 3] 응답 파싱 — 사내 스펙의 응답 구조에 맞춰 수정
    # ------------------------------------------------------------------
    def _parse_response(self, raw: Dict[str, Any]) -> LLMResponse:
        # --- OpenAI 호환 형태 ---
        if "choices" in raw:
            choice = raw["choices"][0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            calls = [
                ToolCall(
                    id=tc.get("id", f"call_{i}"),
                    name=tc["function"]["name"],
                    arguments=_loads_lenient(tc["function"].get("arguments")),
                )
                for i, tc in enumerate(msg.get("tool_calls") or [])
            ]
            u = raw.get("usage") or {}
            return LLMResponse(
                text=text,
                tool_calls=calls,
                usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
                raw=raw,
                finish_reason=choice.get("finish_reason", "stop"),
            )

        # --- 자체 포맷 예시 ---
        # 사내 응답이 예를 들어 {"result": {"text": "...", "tokens": {...}}} 라면
        # 아래처럼 꺼내면 된다. 실제 필드명으로 바꿔서 사용할 것.
        for key in ("result", "data", "output", "response"):
            if key in raw and isinstance(raw[key], dict):
                body = raw[key]
                text = body.get("text") or body.get("content") or body.get("message") or ""
                tok = body.get("tokens") or body.get("usage") or {}
                return LLMResponse(
                    text=text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
                    usage=Usage(
                        tok.get("input", tok.get("prompt_tokens", 0)),
                        tok.get("output", tok.get("completion_tokens", 0)),
                    ),
                    raw=raw,
                )

        # 최상위에 바로 텍스트가 오는 경우
        for key in ("text", "content", "answer", "message"):
            if isinstance(raw.get(key), str):
                return LLMResponse(text=raw[key], raw=raw)

        raise LLMError(
            "사내 AI 응답 구조를 해석하지 못했습니다. corp.py의 [EDIT 3]을 "
            f"실제 스펙에 맞게 수정하세요. 받은 키: {sorted(raw.keys())}"
        )

    # ------------------------------------------------------------------
    # 아래는 수정할 필요 없음
    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover
                raise LLMError("httpx가 필요합니다: pip install httpx") from exc
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolSpec]] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        payload = self._build_request(
            messages, tools, model or self.default_model, temperature, max_tokens
        )
        url = f"{self.base_url}/chat/completions"  # ← 사내 엔드포인트 경로로 수정
        client = self._get_client()

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
                if resp.status_code in RETRYABLE_STATUS:
                    raise LLMTransientError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                return self._parse_response(resp.json())
            except LLMError as exc:
                if not isinstance(exc, LLMTransientError):
                    raise
                last_exc = exc
            except Exception as exc:  # 네트워크/타임아웃
                last_exc = exc

            backoff = min(2 ** attempt, 8) + (attempt * 0.1)
            log.warning("사내 AI 호출 실패 (%s/%s), %.1fs 후 재시도: %s",
                        attempt + 1, self.max_retries, backoff, last_exc)
            await asyncio.sleep(backoff)

        raise LLMTransientError(f"사내 AI 호출 {self.max_retries}회 실패: {last_exc}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _loads_lenient(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (ValueError, TypeError):
        return {"_raw": str(value)}
