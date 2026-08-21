"""폭주 제어 가드 검증."""
from __future__ import annotations

from app.core.guards import PingPongDetector, TraceBudget, TurnGate


def check(label, actual, expected):
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  기대={expected} 실제={actual}"))
    return 0 if ok else 1


def run() -> int:
    f = 0
    gate = TurnGate()
    b = TraceBudget(trace_id="t1", max_depth=3)

    # (3) 자기 자신 트리거 차단
    f += check("자기 메시지로는 안 깨어남",
               gate.check(agent_id="a1", author_type="agent", author_id="a1", budget=b).allowed, False)
    f += check("남의 메시지로는 깨어남",
               gate.check(agent_id="a1", author_type="agent", author_id="a2", budget=b).allowed, True)
    f += check("비활성 에이전트 차단",
               gate.check(agent_id="a1", author_type="human", author_id="h1", budget=b,
                          agent_enabled=False).allowed, False)

    # (1) 멘션 깊이 — 한계에 닿으면 mention_agent 능력만 박탈
    d0 = TraceBudget("t2", depth=0, max_depth=3)
    d3 = TraceBudget("t2", depth=3, max_depth=3)
    d4 = TraceBudget("t2", depth=4, max_depth=3)
    f += check("depth 0: 남을 부를 수 있음", d0.can_mention_agents, True)
    f += check("depth 3(한계): 못 부름", d3.can_mention_agents, False)
    f += check("depth 3(한계): 턴 자체는 허용", d3.exhausted() is None, True)
    f += check("depth 4(초과): 턴 차단", d4.exhausted() is not None, True)

    # child()가 예산을 공유하는지
    c = d0.child().child()
    f += check("child로 depth 누적", c.depth, 2)
    f += check("child가 한계값 상속", c.max_depth, 3)

    # 토큰/실행 예산
    tb = TraceBudget("t3", max_tokens=1000)
    tb.tokens_spent = 1200
    f += check("토큰 예산 소진 시 차단", tb.exhausted() is not None, True)
    rb = TraceBudget("t4", max_runs=3)
    rb.runs_spent = 3
    f += check("실행 횟수 초과 시 차단", rb.exhausted() is not None, True)

    # (1') 핑퐁 A→B→A→B→A→B
    det = PingPongDetector(max_repeats=2)
    g2 = TurnGate(det)
    bb = TraceBudget("t5", max_depth=99)
    seq = ["a", "b", "a", "b", "a", "b"]
    blocked_at = None
    for i, who in enumerate(seq):
        dec = g2.check(agent_id=who, author_type="agent", author_id="other", budget=bb)
        if not dec.allowed and blocked_at is None:
            blocked_at = i
            break
        g2.commit("t5", who)
    f += check("핑퐁이 depth 한계 전에 차단됨", blocked_at is not None, True)
    print(f"       → {blocked_at}번째 홉에서 차단 (depth 한계 99인데도)")

    # 정상적인 릴레이 A→B→C 는 막히면 안 됨
    det2 = PingPongDetector()
    g3 = TurnGate(det2)
    bb2 = TraceBudget("t6", max_depth=99)
    relay_ok = True
    for who in ["a", "b", "c", "d", "a"]:
        if not g3.check(agent_id=who, author_type="agent", author_id="x", budget=bb2).allowed:
            relay_ok = False
            break
        g3.commit("t6", who)
    f += check("정상 릴레이(A→B→C→D→A)는 통과", relay_ok, True)
    return f


if __name__ == "__main__":
    import sys
    n = run()
    print("-" * 60)
    print("모두 통과" if n == 0 else f"{n}건 실패")
    sys.exit(1 if n else 0)
