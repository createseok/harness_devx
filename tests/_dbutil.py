"""테스트용 DB 해석 + 안전장치.

테스트는 TRUNCATE 를 한다. 개발 DB 를 가리킨 채 돌면 작업 데이터가 날아간다.
(실제로 한 번 날렸다.) 그래서 DB 이름이 명시적으로 테스트용이 아니면
**실행 자체를 거부한다.**

우선순위:
  1. TEST_DATABASE_URL 이 있으면 그것
  2. DATABASE_URL 이 있으면 DB 이름에 _test 를 붙인 것
  3. 기본값 postgresql+asyncpg://genteam:genteam@127.0.0.1/genteam_test
"""
from __future__ import annotations

import os
import re
from typing import Optional

DEFAULT = "postgresql+asyncpg://genteam:genteam@127.0.0.1/genteam_test"
_SAFE = re.compile(r"(_test|_tests|test_)", re.IGNORECASE)


def db_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?")[0]


def resolve_test_url() -> Optional[str]:
    """테스트가 써도 되는 URL 을 돌려준다. 없으면 None (건너뛰기)."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        base = os.getenv("DATABASE_URL")
        if not base:
            return None
        name = db_name(base)
        url = base[: -len(name)] + (name if _SAFE.search(name) else name + "_test")
    return url


def assert_safe(url: str) -> None:
    """TRUNCATE 전에 반드시 부른다."""
    name = db_name(url)
    if not _SAFE.search(name):
        raise SystemExit(
            f"거부: '{name}' 은 테스트 DB 로 보이지 않습니다.\n"
            f"  이 테스트는 테이블을 TRUNCATE 하므로 개발 DB 에서 돌리면 안 됩니다.\n"
            f"  TEST_DATABASE_URL 에 이름이 _test 로 끝나는 DB 를 지정하세요.\n"
            f"  예:  createdb genteam_test\n"
            f"       TEST_DATABASE_URL={DEFAULT}"
        )


async def fresh_store(url: str):
    """스키마를 만들고 모든 테이블을 비운 SqlStore 를 돌려준다."""
    assert_safe(url)
    from sqlalchemy import text
    from app.store.sql import SqlStore
    store = SqlStore(url)
    await store.create_all()
    async with store.engine.begin() as conn:
        for t in ("agent_runs", "messages", "channel_members", "tasks",
                  "agents", "humans", "channels"):
            await conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
    return store
