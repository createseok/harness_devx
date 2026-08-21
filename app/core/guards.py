"""폭주 제어.

멀티에이전트 시스템이 프로덕션에서 터지는 방식은 대부분 LLM 품질 문제가
아니라 아래 넷 중 하나다:

  1) 무한 멘션 루프   A→B→A→B…  (요금 폭발)
  2) 동시 응답 폭주   한 마디에 에이전트 5명이 동시에 답변
  3) 자기 자신 트리거 에이전트가 쓴 글이 자기를 다시 깨움
  4) 중복 실행       재시도/재배달로 같은 턴이 두 번 실행

이 모듈이 넷 모두를 막는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class TraceBudget:
    """사람의 한 마디에서 시작된 연쇄 전체에 걸리는 예산."""

    trace_id: str
    depth: int = 0

    #: 에이전트→에이전트 홉 최대 횟수. 넘으면 mention_agent 도구가 사라진다.
    max_depth: int = 4
    #: 연쇄 전체 토큰 상한. 넘으면 턴 자체를 시작하지 않는다.
    max_tokens: int = 120_000
    #: 연쇄 전체 에이전트 실행 횟수 상한.
    max_runs: int = 20

    tokens_spent: int = 0
    runs_spent: int = 0

    def child(self) -> "TraceBudget":
        """다음 홉으로 넘길 예산. depth만 증가하고 나머지는 공유된다."""
        return TraceBudget(
            trace_id=self.trace_id,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            max_tokens=self.max_tokens,
            max_runs=self.max_runs,
            tokens_spent=self.tokens_spent,
            runs_spent=self.runs_spent,
        )

    @property
    def can_mention_agents(self) -> bool:
        """깊이 한계에 닿으면 에이전트는 더 이상 남을 부를 수 없다.

        이게 무한 루프를 끊는 핵심 장치다. 턴을 실패시키는 게 아니라
        '남에게 넘기기' 능력만 뺏으므로, 에이전트는 사람에게 보고하고 끝낸다.
        """
        return self.depth < self.max_depth

    def exhausted(self) -> Optional[str]:
        if self.depth > self.max_depth:
            return f"멘션 깊이 한계 초과 (depth={self.depth} > {self.max_depth})"
        if self.tokens_spent >= self.max_tokens:
            return f"토큰 예산 소진 ({self.tokens_spent:,} >= {self.max_tokens:,})"
        if self.runs_spent >= self.max_runs:
            return f"실행 횟수 한계 초과 ({self.runs_spent} >= {self.max_runs})"
        return None


class PingPongDetector:
    """A→B→A→B 같은 짧은 주기의 왕복을 depth 한계보다 먼저 잡아낸다.

    depth 한계만으로도 결국 멈추지만, 두 에이전트가 서로 '고맙습니다'만
    주고받는 걸 4홉이나 지켜볼 이유는 없다.
    """

    def __init__(self, window: int = 6, max_repeats: int = 2) -> None:
        self.window = window
        self.max_repeats = max_repeats
        self._chains: Dict[str, List[str]] = {}

    def record(self, trace_id: str, agent_id: str) -> None:
        chain = self._chains.setdefault(trace_id, [])
        chain.append(agent_id)
        if len(chain) > self.window:
            del chain[0]

    def is_ping_pong(self, trace_id: str, agent_id: str) -> bool:
        chain = self._chains.get(trace_id, []) + [agent_id]
        if len(chain) < 4:
            return False
        # 최근 2-그램이 반복되는지 확인: [A,B,A,B] → 반복
        pair = tuple(chain[-2:])
        repeats = sum(
            1 for i in range(len(chain) - 1)
            if tuple(chain[i:i + 2]) == pair
        )
        return repeats > self.max_repeats

    def forget(self, trace_id: str) -> None:
        self._chains.pop(trace_id, None)


@dataclass
class GateDecision:
    allowed: bool
    reason: str = ""


class TurnGate:
    """에이전트 한 턴을 시작해도 되는지 판정한다."""

    def __init__(self, detector: Optional[PingPongDetector] = None) -> None:
        self.detector = detector or PingPongDetector()

    def check(
        self,
        *,
        agent_id: str,
        author_type: str,
        author_id: str,
        budget: TraceBudget,
        agent_enabled: bool = True,
    ) -> GateDecision:
        # (3) 자기 자신이 쓴 글로는 절대 깨어나지 않는다
        if author_type == "agent" and author_id == agent_id:
            return GateDecision(False, "자기 자신의 메시지")

        if not agent_enabled:
            return GateDecision(False, "비활성화된 에이전트")

        # (1) 예산/깊이
        exhausted = budget.exhausted()
        if exhausted:
            return GateDecision(False, exhausted)

        # (1') 핑퐁
        if self.detector.is_ping_pong(budget.trace_id, agent_id):
            return GateDecision(False, "핑퐁 루프 감지")

        return GateDecision(True)

    def commit(self, trace_id: str, agent_id: str) -> None:
        self.detector.record(trace_id, agent_id)
