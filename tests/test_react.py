"""ReAct 파서 검증: 약한 모델이 실제로 뱉는 변형들을 견디는지 확인."""
from __future__ import annotations

from app.core.react import parse_actions

KNOWN = ["post_message", "reply_in_thread", "mention_agent", "finish"]

CASES = [
    (
        "정석 포맷",
        '```action\n{"tool": "post_message", "args": {"text": "안녕하세요"}}\n```',
        [("post_message", "안녕하세요")],
    ),
    (
        "action 블록 2개 연속",
        '```action\n{"tool":"post_message","args":{"text":"시작합니다"}}\n```\n'
        '```action\n{"tool":"finish","args":{"summary":"완료"}}\n```',
        [("post_message", "시작합니다"), ("finish", None)],
    ),
    (
        "```json 으로 열었을 때",
        '```json\n{"tool":"post_message","args":{"text":"json펜스"}}\n```',
        [("post_message", "json펜스")],
    ),
    (
        "펜스 없는 날것 JSON",
        '{"tool": "post_message", "args": {"text": "펜스없음"}}',
        [("post_message", "펜스없음")],
    ),
    (
        "trailing comma + 주석",
        '```action\n{\n  // 채널에 알린다\n  "tool": "post_message",\n  "args": {"text": "더러운JSON",},\n}\n```',
        [("post_message", "더러운JSON")],
    ),
    (
        "args 없이 평평하게 뱉음",
        '```action\n{"tool":"post_message","text":"평평함"}\n```',
        [("post_message", "평평함")],
    ),
    (
        "OpenAI function 스타일 흉내",
        '```action\n{"function":{"name":"post_message","arguments":"{\\"text\\":\\"중첩\\"}"}}\n```',
        [("post_message", "중첩")],
    ),
    (
        "JSON 배열로 여러 개",
        '```action\n[{"tool":"post_message","args":{"text":"하나"}},{"tool":"finish","args":{}}]\n```',
        [("post_message", "하나"), ("finish", None)],
    ),
    (
        "<action> 태그",
        '<action>{"tool":"post_message","args":{"text":"태그형"}}</action>',
        [("post_message", "태그형")],
    ),
    (
        "thinking 흘림 + 정상 블록",
        '<thinking>음 이렇게 해야지</thinking>\n```action\n{"tool":"post_message","args":{"text":"사고제거"}}\n```',
        [("post_message", "사고제거")],
    ),
    (
        "도구 이름 오타",
        '```action\n{"tool":"post_msg","args":{"text":"오타보정"}}\n```',
        [("post_message", "오타보정")],
    ),
    (
        "형식 완전 무시하고 자연어만 (관대 모드)",
        "네, 확인했습니다. 제가 처리하겠습니다.",
        [("post_message", "네, 확인했습니다. 제가 처리하겠습니다.")],
    ),
    (
        "자연어 + action 혼합 → action만 채택",
        '먼저 인사를 하겠습니다.\n```action\n{"tool":"post_message","args":{"text":"혼합"}}\n```',
        [("post_message", "혼합")],
    ),
    (
        "존재하지 않는 도구는 무시",
        '```action\n{"tool":"launch_missiles","args":{}}\n```\n'
        '```action\n{"tool":"finish","args":{}}\n```',
        [("finish", None)],
    ),
]


def run() -> int:
    failures = 0
    for label, raw, expected in CASES:
        calls, leftover, warns = parse_actions(raw, known_tools=KNOWN)
        got = [(c.name, c.arguments.get("text")) for c in calls]
        ok = got == expected
        if not ok:
            failures += 1
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {label}")
        if not ok:
            print(f"       기대: {expected}")
            print(f"       실제: {got}")
        if warns:
            print(f"       경고: {warns[0]}")
    return failures


if __name__ == "__main__":
    import sys
    n = run()
    print("-" * 60)
    print(f"{len(CASES) - n}/{len(CASES)} 통과")
    sys.exit(1 if n else 0)
