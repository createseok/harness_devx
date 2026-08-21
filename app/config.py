"""환경설정. 모든 튜닝 파라미터를 한곳에 모은다."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- 사내 AI ---
    corp_ai_base_url: str = os.getenv("CORP_AI_BASE_URL", "")
    corp_ai_api_key: str = os.getenv("CORP_AI_API_KEY", "")
    corp_ai_model: str = os.getenv("CORP_AI_MODEL", "")
    #: 사내 AI가 네이티브 tool/function calling을 지원하는가.
    #: 모르면 False로 두면 된다 (ReAct 폴백이 어떤 모델에서도 동작한다).
    corp_ai_native_tools: bool = _bool("CORP_AI_NATIVE_TOOLS", False)
    corp_ai_timeout: float = float(os.getenv("CORP_AI_TIMEOUT", "120"))
    corp_ai_max_retries: int = _int("CORP_AI_MAX_RETRIES", 3)

    # --- 저장소 ---
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://localhost/genteam"
    )
    use_memory_store: bool = _bool("USE_MEMORY_STORE", False)

    # --- 폭주 제어 (프로덕션에서 가장 자주 조정할 값들) ---
    max_mention_depth: int = _int("MAX_MENTION_DEPTH", 4)
    max_trace_tokens: int = _int("MAX_TRACE_TOKENS", 120_000)
    max_trace_runs: int = _int("MAX_TRACE_RUNS", 20)
    max_agent_steps: int = _int("MAX_AGENT_STEPS", 8)
    max_concurrency: int = _int("MAX_CONCURRENCY", 8)

    def validate(self) -> None:
        missing = [
            name for name, val in (
                ("CORP_AI_BASE_URL", self.corp_ai_base_url),
                ("CORP_AI_API_KEY", self.corp_ai_api_key),
                ("CORP_AI_MODEL", self.corp_ai_model),
            ) if not val
        ]
        if missing:
            raise RuntimeError(
                "사내 AI 환경변수가 없습니다: " + ", ".join(missing)
                + "\n.env.example 를 참고해 .env 를 만드세요. "
                "(연동 전에 먼저 돌려보려면 USE_MEMORY_STORE=1 + scripts/demo.py)"
            )


settings = Settings()
