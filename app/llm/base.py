"""LLM Provider 추상화.

에이전트 런타임은 이 인터페이스만 알고, 사내 AI의 실제 스펙은
`corp.py` 한 파일에만 갇혀 있다. 모델/엔드포인트가 바뀌어도
런타임 코드는 건드릴 필요가 없다.
"""
from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChatMessage:
    role: str                      # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None     # 발화자 표시용 (멀티 스피커 채널에서 중요)
    tool_call_id: Optional[str] = None
    #: provider 고유 content 블록. 네이티브 tool calling에서 assistant 턴의
    #: tool_use 블록을 손실 없이 되돌려보내기 위해 쓴다. ReAct 경로에서는 항상 None.
    blocks: Optional[List[Any]] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]     # JSON Schema (object)

    def to_openai(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_prompt_block(self) -> str:
        """네이티브 tool calling이 없는 모델에게 프롬프트로 주입할 형태."""
        return f"- {self.name}: {self.description}\n  args schema: {json.dumps(self.parameters, ensure_ascii=False)}"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw: Any = None
    finish_reason: str = "stop"


class LLMError(RuntimeError):
    """재시도해도 소용없는 오류 (스펙 불일치, 인증 실패 등)."""


class LLMTransientError(LLMError):
    """재시도하면 성공할 수 있는 오류 (타임아웃, 429, 5xx)."""


class LLMProvider(abc.ABC):
    """모든 provider가 구현해야 하는 인터페이스."""

    #: 네이티브 tool/function calling을 지원하는가.
    #: False면 런타임이 자동으로 ReAct(프롬프트 기반) 모드로 전환한다.
    supports_native_tools: bool = False

    #: 기본 모델 이름
    default_model: str = ""

    @abc.abstractmethod
    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolSpec]] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        ...

    async def aclose(self) -> None:
        return None
