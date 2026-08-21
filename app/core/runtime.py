"""에이전트 런타임: 한 턴을 실행하는 루프.

    컨텍스트 조립 → LLM 호출 → 툴 실행 → 관측 되먹임 → (반복) → finish

네이티브 tool calling 지원 여부에 따라 두 경로가 갈리지만,
호출부는 그 차이를 몰라도 된다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.context import build_context_block
from app.core.guards import TraceBudget
from app.core.models import (
    Agent, AgentRun, Channel, Message, MessageKind, RunStatus, new_id,
)
from app.core.react import parse_actions, render_system_prompt
from app.core.tools import ToolContext, ToolRegistry
from app.llm.base import ChatMessage, LLMProvider, ToolCall, Usage
from app.store.base import Store

log = logging.getLogger(__name__)


def _native_blocks(resp) -> Optional[List[Any]]:
    """provider가 원본 content 블록을 실어보냈으면 그대로 돌려준다."""
    raw = getattr(resp, "raw", None)
    if isinstance(raw, dict):
        return raw.get("content")
    return None


@dataclass
class TurnResult:
    run: AgentRun
    emitted: List[Message] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    steps: int = 0


class AgentRuntime:
    def __init__(
        self,
        store: Store,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        log_tool_calls: bool = True,
    ) -> None:
        self.store = store
        self.provider = provider
        self.registry = registry
        self.log_tool_calls = log_tool_calls

    async def run_turn(
        self,
        agent: Agent,
        channel: Channel,
        trigger: Message,
        budget: TraceBudget,
    ) -> Optional[TurnResult]:
        """한 턴 실행. 멱등성 선점에 실패하면 None (이미 실행됨)."""
        run = AgentRun(
            id=new_id("run"),
            agent_id=agent.id,
            channel_id=channel.id,
            trigger_message_id=trigger.id,
            trace_id=budget.trace_id,
            depth=budget.depth,
        )
        if not await self.store.claim_run(run):
            log.info("중복 실행 차단: %s", run.idem_key)
            return None

        ctx = ToolContext(
            store=self.store,
            agent=agent,
            channel_id=channel.id,
            trigger_message=trigger,
            budget=budget,
            thread_id=trigger.thread_id,
        )
        warnings: List[str] = []
        usage_total = Usage()

        try:
            tools = self.registry.specs(budget)
            tool_names = self.registry.names(budget)
            context_block = await build_context_block(self.store, agent, channel, trigger)

            if self.provider.supports_native_tools:
                system = f"{agent.role_prompt}\n\n# 현재 상황\n{context_block}"
            else:
                system = render_system_prompt(agent.role_prompt, tools, context_block)

            convo: List[ChatMessage] = [
                ChatMessage("system", system),
                ChatMessage("user",
                            f"[{trigger.author_name}] {trigger.text}",
                            name=trigger.author_name),
            ]

            for step in range(agent.max_steps):
                run.steps = step + 1

                resp = await self.provider.chat(
                    convo, tools, model=agent.model, temperature=0.3
                )
                usage_total = usage_total + resp.usage
                budget.tokens_spent += resp.usage.total

                # --- 툴 호출 추출: 네이티브 vs ReAct ---
                if self.provider.supports_native_tools:
                    calls: List[ToolCall] = list(resp.tool_calls)
                    leftover = resp.text or ""
                    if not calls and leftover.strip():
                        # 네이티브 모드에서도 그냥 말만 하는 경우가 있다 → 발화로 처리
                        calls = [ToolCall("native_fallback", "post_message",
                                          {"text": leftover.strip()})]
                        leftover = ""
                else:
                    calls, leftover, warns = parse_actions(
                        resp.text, known_tools=tool_names
                    )
                    warnings.extend(warns)

                if not calls:
                    warnings.append(f"step {step + 1}: 툴 호출을 추출하지 못했습니다.")
                    convo.append(ChatMessage("assistant", resp.text or ""))
                    convo.append(ChatMessage(
                        "user",
                        "형식이 올바르지 않습니다. 반드시 ```action 블록으로만 응답하세요. "
                        f"사용 가능한 도구: {', '.join(tool_names)}",
                    ))
                    continue

                native = self.provider.supports_native_tools
                # 네이티브 모드에서는 assistant 턴의 content 블록을 그대로 보존해야
                # 다음 요청에서 tool_use ↔ tool_result 짝이 맞는다.
                convo.append(ChatMessage(
                    "assistant", resp.text or "",
                    blocks=_native_blocks(resp) if native else None,
                ))

                # --- 툴 실행 ---
                observations: List[str] = []
                for call in calls:
                    result = await self.registry.execute(call.name, call.arguments, ctx)
                    observations.append(f"[{call.name}] {result}")
                    if native:
                        # 호출 1건당 tool_result 1건 — provider가 묶어서 보낸다
                        convo.append(ChatMessage("tool", result, tool_call_id=call.id))
                    if self.log_tool_calls:
                        await self._write_tool_log(ctx, call, result)
                    if ctx.finished:
                        break

                if not native:
                    convo.append(ChatMessage(
                        "user", "도구 실행 결과:\n" + "\n".join(observations)
                    ))

                if ctx.finished:
                    break

                # 예산이 도중에 소진되면 즉시 중단
                exhausted = budget.exhausted()
                if exhausted:
                    warnings.append(f"턴 중단: {exhausted}")
                    break
            else:
                warnings.append(
                    f"max_steps({agent.max_steps}) 도달 — finish 없이 종료했습니다."
                )

            # 아무 말도 안 하고 끝난 턴은 사람에게 침묵으로 보인다 → 흔적을 남긴다
            if not ctx.emitted and not ctx.finished:
                await ctx.emit(
                    "(요청을 처리하지 못했습니다. 좀 더 구체적으로 알려주시겠어요?)"
                )

            run.status = RunStatus.DONE

        except Exception as exc:
            log.exception("에이전트 턴 실패: %s", agent.name)
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            await ctx.emit(f"⚠️ 처리 중 오류가 발생했습니다: {run.error}")

        finally:
            run.prompt_tokens = usage_total.prompt_tokens
            run.completion_tokens = usage_total.completion_tokens
            budget.runs_spent += 1
            await self.store.finish_run(run)

        return TurnResult(run=run, emitted=list(ctx.emitted),
                          warnings=warnings, steps=run.steps)

    async def _write_tool_log(self, ctx: ToolContext, call: ToolCall, result: str) -> None:
        """관측용 내부 기록. kind=TOOL_LOG 라서 다른 에이전트를 깨우지 않는다."""
        from app.core.models import MemberType
        await self.store.add_message(Message(
            id=new_id("log"),
            channel_id=ctx.channel_id,
            author_type=MemberType.AGENT,
            author_id=ctx.agent.id,
            author_name=ctx.agent.name,
            text=f"{call.name}({call.arguments}) → {result[:300]}",
            kind=MessageKind.TOOL_LOG,
            trace_id=ctx.budget.trace_id,
            depth=ctx.budget.depth,
            caused_by=ctx.trigger_message.id,
        ))
