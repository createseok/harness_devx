"""Provider 팩토리 — LLM 백엔드 교체 지점.

교체는 .env 한 줄이다:

    LLM_PROVIDER=claude_cli   # 지금: claude -p, API 키 불필요
    LLM_PROVIDER=anthropic    # Anthropic API (네이티브 tool calling)
    LLM_PROVIDER=corp         # 나중: 사내 AI
    LLM_PROVIDER=mock         # 테스트/데모

애플리케이션 코드는 어느 것이 쓰이는지 전혀 모른다. 런타임·디스패처·
툴 레지스트리 어디에도 provider 이름이 등장하지 않는다.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from app.llm.base import LLMProvider

log = logging.getLogger(__name__)

PROVIDERS = ("claude_cli", "anthropic", "corp", "mock")


def build_provider(name: Optional[str] = None, settings=None) -> LLMProvider:
    if settings is None:
        from app.config import settings as default_settings
        settings = default_settings
    name = (name or settings.llm_provider or "claude_cli").strip().lower()

    builder = _BUILDERS.get(name)
    if builder is None:
        raise ValueError(
            f"알 수 없는 LLM_PROVIDER='{name}'. 사용 가능: {', '.join(PROVIDERS)}"
        )
    provider = builder(settings)
    log.info("LLM provider=%s model=%s native_tools=%s",
             name, provider.default_model, provider.supports_native_tools)
    return provider


def _claude_cli(s) -> LLMProvider:
    from app.llm.claude_cli import ClaudeCliProvider
    return ClaudeCliProvider(
        cli_path=s.claude_cli_path,
        default_model=s.claude_cli_model,
        timeout=s.claude_cli_timeout,
        max_budget_usd=s.claude_cli_max_budget_usd,
        extra_args=[a for a in (s.claude_cli_extra_args or "").split() if a],
        builtin_tools=s.claude_cli_tools,
        max_turns=s.claude_cli_max_turns,
    )


def _anthropic(s) -> LLMProvider:
    from app.llm.anthropic_api import AnthropicProvider
    return AnthropicProvider(
        api_key=s.anthropic_api_key or None,
        default_model=s.anthropic_model,
        effort=s.anthropic_effort,
    )


def _corp(s) -> LLMProvider:
    from app.llm.corp import CorpProvider
    s.validate_corp()
    return CorpProvider(
        base_url=s.corp_ai_base_url,
        api_key=s.corp_ai_api_key,
        default_model=s.corp_ai_model,
        supports_native_tools=s.corp_ai_native_tools,
        timeout=s.corp_ai_timeout,
        max_retries=s.corp_ai_max_retries,
    )


def _mock(s) -> LLMProvider:
    from app.llm.mock import ScriptedProvider
    return ScriptedProvider({})


_BUILDERS: Dict[str, Callable[[object], LLMProvider]] = {
    "claude_cli": _claude_cli,
    "anthropic": _anthropic,
    "corp": _corp,
    "mock": _mock,
}
