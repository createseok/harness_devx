"""채널 롤링 요약 (Phase 4).

문제: 컨텍스트 조립은 최근 N개만 원문으로 넣는다. 크기는 이미 유계였다 —
문제는 크기가 아니라 **윈도 밖 정보가 통째로 사라지는 것**이다.
실측: 400건 채널에서 컨텍스트는 1,811자로 일정했지만, 초기에 확정된
결정은 프롬프트에 흔적도 남지 않았다. 나중에 합류한 에이전트는 과거를
전혀 모른 채 일하게 된다.

해법: 윈도 밖으로 밀려나는 메시지를 버리지 말고 요약으로 남긴다.
같은 컨텍스트 예산으로 채널 전체를 커버한다.

    [ 요약 (아주 오래된 것 전부) ] + [ 최근 N개 원문 ]

**증분 생성이 핵심이다.** 매번 채널 전체를 다시 읽어 요약하면 요약 비용이
채널 길이에 비례해 커져서 원래 문제를 그대로 옮겨놓을 뿐이다.
그래서 이전 요약 + 그 이후 새 메시지만 읽어 다음 요약을 만든다.

    새요약 = f(이전요약, 이전요약 이후의 새 메시지들)

이러면 요약 1회 비용이 채널 길이와 무관하게 일정하다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from app.core.models import ChannelSummary, Message, new_id
from app.llm.base import ChatMessage, LLMProvider
from app.store.base import Store

log = logging.getLogger(__name__)

#: 최근 이 개수만 원문으로 컨텍스트에 넣는다
RECENT_WINDOW = 15
#: 요약 밖 메시지가 이보다 많이 쌓이면 요약을 갱신한다
SUMMARIZE_AFTER = 20
#: 한 번의 LLM 호출에서 읽을 최대 메시지 수 (한 번에 너무 큰 요청 방지)
MAX_BATCH = 120
#: 밀린 백로그를 따라잡기 위해 연속으로 돌릴 최대 횟수.
#: 배치 상한 때문에 한 번에 다 못 따라잡는 경우가 있다.
MAX_CATCHUP_PASSES = 4
#: 요약문 자체의 목표 길이. 이걸 넘기면 요약이 원문만큼 커진다
TARGET_CHARS = 1200

_PROMPT = """\
너는 팀 채널의 기록 담당이다. 아래 대화를 요약해라.

이 요약은 나중에 합류하는 팀원이 **과거를 대신 읽는 용도**다. 그 사람이
이것만 읽고 바로 일에 투입될 수 있어야 한다.

반드시 남길 것:
- 확정된 결정과 그 이유
- 파악된 사실 (숫자, 원인, 확인된 것)
- 누가 무엇을 맡고 있는지
- 아직 안 풀린 것, 막혀 있는 것 (블로커)
- 사람에게 요청해둔 것

버릴 것:
- 인사, 맞장구, 진행 예고 ("확인해보겠습니다" 같은 말)
- 같은 내용의 반복
- 결론 없이 흘러간 논의 과정

형식: 평문 단락. 소제목·번호목록·표를 쓰지 않는다. {target}자 이내.
확정되지 않은 것은 "미확정"이라고 명시한다. 없는 내용을 지어내지 않는다.
"""


class Summarizer:
    def __init__(self, store: Store, provider: LLMProvider, *,
                 summarize_after: int = SUMMARIZE_AFTER,
                 max_batch: int = MAX_BATCH,
                 target_chars: int = TARGET_CHARS,
                 max_catchup_passes: int = MAX_CATCHUP_PASSES) -> None:
        self.store = store
        self.provider = provider
        self.summarize_after = summarize_after
        self.max_batch = max_batch
        self.target_chars = target_chars
        self.max_catchup_passes = max_catchup_passes
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, channel_id: str) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def needs_update(self, channel_id: str) -> bool:
        prev = await self.store.get_summary(channel_id)
        after = prev.up_to_created_at if prev else None
        pending = await self.store.count_messages_after(channel_id, after)
        # 최근 N개는 어차피 원문으로 들어가므로 그만큼은 요약할 필요가 없다
        return pending - RECENT_WINDOW >= self.summarize_after

    async def maybe_update(self, channel_id: str) -> Optional[ChannelSummary]:
        """필요하면 요약을 갱신한다. 밀린 백로그는 따라잡을 때까지 반복한다.

        배치 상한(max_batch) 때문에 한 번에 다 못 따라잡는 경우가 있다.
        한 번만 돌고 끝내면 요약이 영원히 뒤처진 채로 남는다.
        에이전트 턴을 막지 않도록 백그라운드에서 호출할 것.
        """
        if not await self.needs_update(channel_id):
            return None
        async with self._lock(channel_id):
            latest = None
            for _ in range(self.max_catchup_passes):
                # 락 대기 중에 다른 태스크가 이미 했을 수 있다
                if not await self.needs_update(channel_id):
                    break
                result = await self._update(channel_id)
                if result is None:      # 실패했거나 더 흡수할 게 없다
                    break
                latest = result
            return latest

    async def _update(self, channel_id: str) -> Optional[ChannelSummary]:
        prev = await self.store.get_summary(channel_id)
        after = prev.up_to_created_at if prev else None
        pending = await self.store.messages_after(channel_id, after, limit=self.max_batch)

        # 최근 N개는 원문으로 들어가므로 요약 대상에서 뺀다.
        # 이걸 빼지 않으면 같은 내용이 요약과 원문에 이중으로 실린다.
        target = pending[:-RECENT_WINDOW] if len(pending) > RECENT_WINDOW else []
        if not target:
            return None

        body = "\n".join(f"[{m.author_name}] {m.text}" for m in target)
        parts = []
        if prev:
            parts.append(f"# 지금까지의 요약\n{prev.text}")
        parts.append(f"# 새로 추가된 대화\n{body}")
        # 1세대에는 "이전 요약도 유지하라"고 하면 안 된다.
        # 없는 것을 찾다가 "앞선 요약본은 전달받지 못했다" 같은 변명을 요약에 남긴다.
        if prev:
            parts.append("\n위 둘을 합쳐 하나의 요약으로 다시 써라. "
                         "이전 요약의 내용을 유지하되, 새 대화로 뒤집힌 것은 갱신해라.")
        else:
            parts.append("\n위 대화를 하나의 요약으로 써라.")
        # 메타 코멘트 금지 — 요약은 그 자체로 읽히는 글이어야 한다
        parts.append("요약문에 대화 자체나 요약 과정에 대한 언급을 넣지 마라 "
                     "(예: '앞선 요약은 없었다', '아래는 새 대화 기준이다'). "
                     "곧바로 내용부터 쓴다.")

        try:
            resp = await self.provider.chat(
                [ChatMessage("system", _PROMPT.format(target=self.target_chars)),
                 ChatMessage("user", "\n\n".join(parts))],
                max_tokens=1500,
            )
        except Exception as exc:
            # 요약 실패가 에이전트 동작을 막아서는 안 된다
            log.warning("채널 %s 요약 실패 (다음 기회에 재시도): %s", channel_id, exc)
            return None

        text = (resp.text or "").strip()
        if not text:
            log.warning("채널 %s 요약이 비어 있어 건너뜁니다", channel_id)
            return None

        last = target[-1]
        summary = ChannelSummary(
            id=new_id("sum"), channel_id=channel_id, text=text,
            up_to_message_id=last.id, up_to_created_at=last.created_at,
            covered_count=(prev.covered_count if prev else 0) + len(target),
        )
        await self.store.save_summary(summary)
        log.info("채널 %s 요약 갱신: %d건 흡수 (누적 %d건, %d자)",
                 channel_id, len(target), summary.covered_count, len(text))
        return summary
