"""ReAct 폴백: 네이티브 tool calling이 없는 모델에서 툴을 쓰게 만드는 계층.

핵심은 **관대한 파싱**이다. 사내 모델은 대체로 GPT-4급 지시 준수력이 없으므로
포맷을 조금씩 어긴다. 어긴다고 턴을 실패시키면 제품이 못 쓰게 된다.
그래서 5단계로 점점 느슨하게 시도한다.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from app.llm.base import ToolCall, ToolSpec

# ```action / ```json / ``` 로 열린 모든 펜스를 잡는다
_FENCE = re.compile(r"```[ \t]*(?:action|json|tool|tool_call)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
# <action>...</action> 형태도 허용
_XML_TAG = re.compile(r"<action>\s*(.*?)\s*</action>", re.DOTALL | re.IGNORECASE)
# 모델이 자주 흘리는 사고 흔적 제거용
_THINK = re.compile(r"<(thinking|thought|think)>.*?</\1>", re.DOTALL | re.IGNORECASE)


FORMAT_INSTRUCTIONS = """\
# 응답 형식 (반드시 지킬 것)

너는 자연어로 답하지 않는다. 반드시 아래 형식의 **action 블록**으로만 응답한다.

```action
{"tool": "도구이름", "args": {"인자": "값"}}
```

규칙:
- 모든 응답에는 action 블록이 최소 1개 있어야 한다.
- 여러 도구를 연달아 쓰려면 action 블록을 여러 개 쓴다.
- 채널에 무언가 말하려면 반드시 `post_message` 또는 `reply_in_thread`를 쓴다.
  action 블록 밖의 글은 아무도 보지 못한다.
- 할 일을 마쳤으면 마지막에 반드시 `finish`를 호출한다.
- args는 유효한 JSON이어야 한다. 주석이나 후행 쉼표를 넣지 않는다."""


def render_system_prompt(role_prompt: str, tools: List[ToolSpec], context_block: str) -> str:
    """에이전트의 시스템 프롬프트를 조립한다."""
    tool_block = "\n".join(t.to_prompt_block() for t in tools)
    return f"""{role_prompt}

# 사용 가능한 도구
{tool_block}

{FORMAT_INSTRUCTIONS}

# 현재 상황
{context_block}"""


def parse_actions(
    text: str,
    *,
    known_tools: Optional[List[str]] = None,
    fallback_tool: Optional[str] = "post_message",
    fallback_arg: str = "text",
) -> Tuple[List[ToolCall], str, List[str]]:
    """모델 출력에서 툴 호출을 뽑아낸다.

    Returns:
        (tool_calls, 남은 자연어, 경고 목록)
    """
    warnings: List[str] = []
    if not text:
        return [], "", ["모델이 빈 응답을 반환했습니다."]

    cleaned = _THINK.sub("", text)
    blobs: List[str] = []

    # 1단계: 코드펜스
    for m in _FENCE.finditer(cleaned):
        blobs.append(m.group(1))
    leftover = _FENCE.sub("", cleaned)

    # 2단계: <action> 태그
    if not blobs:
        for m in _XML_TAG.finditer(cleaned):
            blobs.append(m.group(1))
        leftover = _XML_TAG.sub("", cleaned)

    # 3단계: 펜스 없이 날것으로 뱉은 JSON 객체 스캔
    if not blobs:
        raw = _scan_json_objects(cleaned)
        if raw:
            blobs.extend(raw)
            warnings.append("코드펜스 없이 JSON을 반환했습니다(허용됨).")
            leftover = ""

    calls: List[ToolCall] = []
    for i, blob in enumerate(blobs):
        for obj in _iter_objects(blob, warnings):
            call = _to_tool_call(obj, i, known_tools, warnings)
            if call is not None:
                calls.append(call)

    leftover = leftover.strip()

    # 4단계: 툴 호출이 하나도 없으면 → 자연어를 발화로 간주 (관대 모드)
    if not calls and leftover and fallback_tool:
        warnings.append(
            "action 블록이 없어 자연어 전체를 %s 로 처리했습니다." % fallback_tool
        )
        calls.append(ToolCall(id="fallback_0", name=fallback_tool, arguments={fallback_arg: leftover}))
        leftover = ""

    return calls, leftover, warnings


def _iter_objects(blob: str, warnings: List[str]):
    """블록 하나에서 dict를 최대한 뽑아낸다. 배열/JSONL/트레일링 콤마 모두 허용."""
    blob = blob.strip()
    if not blob:
        return
    try:
        parsed = json.loads(blob)
    except ValueError:
        repaired = _repair_json(blob)
        try:
            parsed = json.loads(repaired)
        except ValueError:
            # 블록 안에 여러 객체가 붙어있는 경우
            found = _scan_json_objects(blob)
            if not found:
                warnings.append("JSON 파싱 실패: %s" % blob[:120].replace("\n", " "))
                return
            for f in found:
                try:
                    yield json.loads(f)
                except ValueError:
                    continue
            return

    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                yield item
    elif isinstance(parsed, dict):
        yield parsed


def _to_tool_call(
    obj: Dict[str, Any], idx: int, known_tools: Optional[List[str]], warnings: List[str]
) -> Optional[ToolCall]:
    # 필드명 변형 흡수: tool / name / action / function
    name = obj.get("tool") or obj.get("name") or obj.get("action") or obj.get("function")
    if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
        args = name.get("arguments") or name.get("args") or {}
        name = name.get("name")
    else:
        args = obj.get("args")
        if args is None:
            args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters")
        if args is None:
            args = obj.get("input")
        if args is None:
            # {"tool": "post_message", "text": "..."} 처럼 평평하게 뱉은 경우
            args = {k: v for k, v in obj.items()
                    if k not in ("tool", "name", "action", "function", "thought", "reasoning")}

    if not isinstance(name, str) or not name:
        warnings.append("도구 이름이 없는 블록을 건너뜁니다: %s" % str(obj)[:100])
        return None

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"text": args}
    if not isinstance(args, dict):
        args = {"value": args}

    name = name.strip()
    if known_tools and name not in known_tools:
        # 오타/유사명 보정 (post_msg → post_message)
        match = _closest(name, known_tools)
        if match:
            warnings.append("알 수 없는 도구 '%s' → '%s' 로 보정했습니다." % (name, match))
            name = match
        else:
            warnings.append("알 수 없는 도구 '%s' 를 무시했습니다." % name)
            return None

    return ToolCall(id="call_%d" % idx, name=name, arguments=args)


def _closest(name: str, candidates: List[str]) -> Optional[str]:
    """도구 이름 오타를 보정한다. 3단계: 정규화 일치 → 접두사 → 편집거리.

    cutoff 0.6은 `post_msg`/`postMessage`는 잡고 `launch_missiles` 같은
    환각 도구는 거부하도록 실측해서 정한 값이다.
    """
    lowered = _normalize_tool_name(name)
    normalized = {_normalize_tool_name(c): c for c in candidates}

    if lowered in normalized:
        return normalized[lowered]

    prefix_hits = [orig for norm, orig in normalized.items()
                   if norm.startswith(lowered) or lowered.startswith(norm)]
    if len(prefix_hits) == 1:
        return prefix_hits[0]

    match = difflib.get_close_matches(lowered, list(normalized.keys()), n=1, cutoff=0.6)
    return normalized[match[0]] if match else None


def _normalize_tool_name(name: str) -> str:
    """postMessage / post-message / Post Message → post_message"""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip())
    return re.sub(r"[\s\-]+", "_", s).lower()


def _repair_json(text: str) -> str:
    """LLM이 흔히 내는 JSON 오류를 보정한다."""
    t = text.strip()
    t = re.sub(r"^[a-zA-Z_]+\s*=\s*", "", t)          # `action = {...}`
    t = re.sub(r"//[^\n]*", "", t)                     # 라인 주석
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.DOTALL)   # 블록 주석
    t = re.sub(r",\s*([}\]])", r"\1", t)               # 트레일링 콤마
    t = t.replace("'", '"') if t.count('"') == 0 else t  # 작은따옴표 JSON
    return t


def _scan_json_objects(text: str) -> List[str]:
    """중괄호 균형을 세어 최상위 JSON 객체들을 잘라낸다 (문자열 내부 무시)."""
    out: List[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start : i + 1]
                if '"' in candidate:
                    out.append(candidate)
                start = -1
            elif depth < 0:
                depth = 0
    return out
