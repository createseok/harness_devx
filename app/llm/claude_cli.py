"""`claude -p` (Claude Code 헤드리스 모드) provider.

사내 AI 연동 전에 개발을 진행하기 위한 백엔드. API 키 없이
기존 Claude 구독으로 동작한다.

동작 방식:
  - messages를 하나의 프롬프트로 평탄화해 stdin으로 넘긴다
  - 시스템 프롬프트는 --system-prompt 로 **전체 교체** 한다
    (--append-system-prompt 는 Claude Code의 코딩 에이전트 페르소나가
     남아서 우리 역할 프롬프트와 충돌한다)
  - --tools "" 로 Claude Code 자체 툴(Read/Bash/Edit)을 끈다.
    우리는 순수 텍스트 완성만 필요하고, 툴은 ReAct 레이어가 처리한다
  - --max-turns 1 로 CLI가 자체 에이전트 루프를 돌지 않게 한다

한계:
  - temperature 제어 불가 (CLI가 노출하지 않음)
  - 호출마다 프로세스를 띄우므로 API 대비 느리고 무겁다
  - 네이티브 tool calling을 구조화된 형태로 받을 수 없다 → ReAct 폴백 사용
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from app.llm.base import (
    ChatMessage, LLMError, LLMProvider, LLMResponse, LLMTransientError,
    ToolSpec, Usage,
)

log = logging.getLogger(__name__)

#: 시스템 프롬프트가 이보다 길면 argv 대신 stdin 프롬프트에 접어넣는다 (ARG_MAX 방어)
_ARGV_SYSTEM_LIMIT = 100_000

#: 재시도해도 절대 성공하지 않는 오류 신호. 종료코드/stderr/stdout JSON 모두에서 본다.
_FATAL_SIGNS = (
    "not logged in", "please run /login", "unauthor", "invalid api key",
    "credit balance", "unknown option", "unrecognized", "no such option",
    "permission denied", "quota",
)

_LOGIN_HINT = (
    "claude CLI에 로그인되어 있지 않습니다.\n"
    "  터미널에서 `claude` 를 실행해 로그인한 뒤 다시 시도하세요.\n"
    "  (헤드리스 모드는 로그인을 대신 처리할 수 없습니다)"
)


def _is_fatal(text: str) -> bool:
    low = (text or "").lower()
    return any(sign in low for sign in _FATAL_SIGNS)


_INSTALL_HINT = (
    "`claude` CLI를 찾을 수 없습니다.\n"
    "  설치:  npm install -g @anthropic-ai/claude-code\n"
    "  확인:  claude --version\n"
    "  경로가 다르면 .env 에 CLAUDE_CLI_PATH=/전체/경로/claude 를 설정하세요."
)


class ClaudeCliProvider(LLMProvider):
    # CLI 프린트 모드는 tool_use 블록을 구조화해서 돌려주지 않는다.
    # ReAct 폴백을 쓰는 게 맞다.
    supports_native_tools = False

    def __init__(
        self,
        *,
        cli_path: str = "claude",
        default_model: str = "opus",
        timeout: float = 180.0,
        max_retries: int = 2,
        max_budget_usd: Optional[float] = None,
        extra_args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.cli_path = cli_path
        self.default_model = default_model
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_budget_usd = max_budget_usd
        self.extra_args = extra_args or []
        self.cwd = cwd
        self.total_cost_usd = 0.0
        self._resolved: Optional[str] = None

    def _resolve_cli(self) -> str:
        if self._resolved is None:
            found = (
                shutil.which(self.cli_path)
                or (self.cli_path if os.path.isfile(self.cli_path) else None)
                or _search_node_managers()
            )
            if not found:
                raise LLMError(_INSTALL_HINT)
            self._resolved = found
            log.info("claude CLI: %s", found)
        return self._resolved

    @staticmethod
    def _flatten(messages: List[ChatMessage]) -> tuple:
        """(system_prompt, user_prompt) 로 분리해 평탄화한다."""
        system_parts: List[str] = []
        convo: List[str] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "tool":
                convo.append(f"[도구 실행 결과]\n{m.content}")
            else:
                speaker = m.name or ("사용자" if m.role == "user" else "나")
                convo.append(f"[{speaker}]\n{m.content}")
        return "\n\n".join(system_parts), "\n\n".join(convo)

    def _build_argv(self, system: str, model: str) -> List[str]:
        argv = [
            self._resolve_cli(),
            "-p",
            "--output-format", "json",
            "--model", model,
            # Claude Code 자체 툴을 완전히 끈다 — 우리는 텍스트 완성만 필요하다
            "--tools", "",
            # CLI가 자체 에이전트 루프를 돌면 우리 런타임과 이중 루프가 된다
            "--max-turns", "1",
            "--no-session-persistence",
        ]
        if system and len(system) <= _ARGV_SYSTEM_LIMIT:
            argv += ["--system-prompt", system]
        if self.max_budget_usd:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        argv += self.extra_args
        return argv

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolSpec]] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,   # CLI가 노출하지 않음 — 무시된다
        max_tokens: int = 2048,
    ) -> LLMResponse:
        system, user_prompt = self._flatten(messages)
        if system and len(system) > _ARGV_SYSTEM_LIMIT:
            # 너무 길면 argv 대신 본문에 접어넣는다
            user_prompt = f"{system}\n\n---\n\n{user_prompt}"
            system = ""

        argv = self._build_argv(system, model or self.default_model)

        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._run_once(argv, user_prompt)
            except LLMTransientError as exc:
                last = exc
                if attempt < self.max_retries:
                    backoff = 2 ** attempt
                    log.warning("claude CLI 실패 (%s/%s), %ss 후 재시도: %s",
                                attempt + 1, self.max_retries + 1, backoff, exc)
                    await asyncio.sleep(backoff)
        raise LLMTransientError(f"claude CLI 호출 실패: {last}")

    async def _run_once(self, argv: List[str], prompt: str) -> LLMResponse:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise LLMTransientError(f"claude CLI 타임아웃 ({self.timeout}s)")

        err = stderr.decode(errors="replace").strip()
        out = stdout.decode(errors="replace")

        # 중요: CLI는 실패해도 종료코드 1 + **stdout에 구조화된 JSON** 을 남긴다.
        # 종료코드만 보고 stdout을 버리면 "Not logged in" 같은 실제 원인을 잃는다.
        if out.strip():
            return self._parse(out, err)

        if proc.returncode != 0:
            msg = f"claude CLI 종료코드 {proc.returncode}: {err[:400] or '(stdout/stderr 모두 비어 있음)'}"
            if _is_fatal(err):
                raise LLMError(msg + f"\n실행한 명령: {' '.join(argv[:8])} …")
            raise LLMTransientError(msg)

        raise LLMTransientError(f"claude CLI가 빈 응답을 반환했습니다. stderr: {err[:200]}")

    def _parse(self, raw_stdout: str, stderr: str) -> LLMResponse:
        text_out = raw_stdout.strip()
        if not text_out:
            raise LLMTransientError(f"claude CLI가 빈 응답을 반환했습니다. stderr: {stderr[:200]}")

        try:
            payload = json.loads(text_out)
        except ValueError:
            # --output-format json 이 아닌 채로 돌았거나 형식이 바뀐 경우 → 평문으로 취급
            log.debug("claude CLI가 JSON이 아닌 출력을 반환 — 평문으로 처리")
            return LLMResponse(text=text_out, raw={"stdout": text_out})

        # CLI는 인증 실패도 종료코드 0 + is_error:true 로 돌려준다.
        # 이걸 transient 로 취급하면 무의미한 재시도만 반복하게 된다.
        if payload.get("is_error"):
            detail = payload.get("result")
            if not isinstance(detail, str):
                detail = str(payload.get("error") or payload)[:400]
            if "login" in detail.lower() or "not logged in" in detail.lower():
                raise LLMError(f"{_LOGIN_HINT}\n  CLI 응답: {detail}")
            if _is_fatal(detail):
                raise LLMError(f"claude CLI 오류(재시도 무의미): {detail}")
            raise LLMTransientError(f"claude CLI 오류: {detail}")

        result = payload.get("result", payload)

        # result 는 버전에 따라 문자열이거나 객체다 — 둘 다 받는다
        if isinstance(result, str):
            text = result
            usage_src: Dict[str, Any] = payload.get("usage") or {}
            cost = payload.get("total_cost_usd") or 0.0
        else:
            text = _extract_text(result)
            usage_src = result.get("usage") or payload.get("usage") or {}
            cost = result.get("total_cost_usd") or payload.get("total_cost_usd") or 0.0

        self.total_cost_usd += float(cost or 0.0)
        return LLMResponse(
            text=text,
            usage=Usage(
                int(usage_src.get("input_tokens", 0) or 0),
                int(usage_src.get("output_tokens", 0) or 0),
            ),
            raw=payload,
            finish_reason=(result.get("stop_reason") if isinstance(result, dict) else None) or "stop",
        )


def _version_key(name: str):
    """'v22.23.2' → (22, 23, 2). 정렬 실패해도 죽지 않게 한다."""
    parts = []
    for chunk in name.lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _search_node_managers() -> Optional[str]:
    """nvm/fnm/volta 로 설치한 claude 를 찾는다.

    파이썬 서브프로세스는 로그인 셸을 거치지 않으므로 nvm이 PATH에 주입한
    경로를 보지 못한다. uvicorn/systemd 로 띄우면 거의 항상 여기 걸린다.
    가장 높은 node 버전을 고른다.
    """
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".nvm", "versions", "node"),
        os.path.join(home, ".fnm", "node-versions"),
        os.path.join(home, ".volta", "bin"),
        os.path.join(home, ".local", "share", "fnm", "node-versions"),
    ]
    candidates = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        direct = os.path.join(root, "claude")
        if os.path.isfile(direct) and os.access(direct, os.X_OK):
            return direct
        for entry in os.listdir(root):
            for sub in ("bin", os.path.join("installation", "bin")):
                path = os.path.join(root, entry, sub, "claude")
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    candidates.append((_version_key(entry), path))
    if candidates:
        candidates.sort()
        return candidates[-1][1]

    for path in ("/usr/local/bin/claude", "/opt/homebrew/bin/claude",
                 os.path.join(home, ".local", "bin", "claude")):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _extract_text(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    for key in ("text", "message", "output"):
        if isinstance(result.get(key), str):
            return result[key]
    return ""
