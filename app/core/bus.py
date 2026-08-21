"""인메모리 이벤트 버스 — SSE/WebSocket 팬아웃용.

여러 프로세스로 확장할 때는 Redis pub/sub 으로 갈아끼운다.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Dict, Set


class EventBus:
    def __init__(self, maxsize: int = 256) -> None:
        self._subs: Dict[str, Set[asyncio.Queue]] = {}
        self._maxsize = maxsize

    def publish(self, topic: str, payload: dict) -> None:
        for q in list(self._subs.get(topic, ())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 느린 구독자 때문에 발행이 막히면 안 된다

    async def subscribe(self, topic: str) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(topic, set()).add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.get(topic, set()).discard(q)


bus = EventBus()
