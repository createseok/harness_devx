"""웹 페이지 읽기.

**어떤 provider 를 쓰든 동작한다.** claude -p 의 내장 WebFetch 와 달리
사내 AI 로 갈아타도 그대로 남는 능력이다.

보안: 에이전트가 URL 을 만들어내므로 SSRF 를 막아야 한다.
클라우드 메타데이터 엔드포인트(169.254.169.254)나 사내망 주소를 읽어
채널에 그대로 게시하면 그게 곧 유출이다.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Tuple
from urllib.parse import urlparse

MAX_BYTES = 2_000_000
TIMEOUT = 20.0
DROP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_TAGS:
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in DROP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t ]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def check_url(url: str) -> Tuple[bool, str]:
    """SSRF 방어. 공개 http(s) 주소만 허용한다."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False, "URL 형식이 올바르지 않습니다."
    if u.scheme not in ("http", "https"):
        return False, "http 또는 https 주소만 읽을 수 있습니다."
    if not u.hostname:
        return False, "호스트가 없는 주소입니다."

    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except OSError:
        return False, f"호스트를 찾을 수 없습니다: {u.hostname}"

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        # 사내망·루프백·링크로컬(클라우드 메타데이터)은 차단
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False, (f"내부 주소({ip})는 읽을 수 없습니다. "
                           "공개된 웹 주소만 허용됩니다.")
    return True, ""


async def fetch_text(url: str, *, max_chars: int = 4000) -> Tuple[bool, str]:
    ok, why = check_url(url)
    if not ok:
        return False, why

    try:
        import httpx
    except ImportError:
        return False, "httpx 가 설치되어 있지 않습니다: pip install httpx"

    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, max_redirects=5,
            headers={"User-Agent": "AgentWorkspace/1.0 (+internal tool)"},
        ) as client:
            resp = await client.get(url)
    except Exception as exc:
        return False, f"가져오지 못했습니다: {type(exc).__name__}: {exc}"

    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code} — 페이지를 읽을 수 없습니다."

    ctype = resp.headers.get("content-type", "")
    body = resp.content[:MAX_BYTES]
    if "html" in ctype:
        parser = _Text()
        parser.feed(body.decode(resp.encoding or "utf-8", errors="replace"))
        text = parser.text()
    elif ctype.startswith("text/") or "json" in ctype:
        text = body.decode(resp.encoding or "utf-8", errors="replace")
    else:
        return False, f"읽을 수 없는 형식입니다: {ctype or '알 수 없음'}"

    if not text.strip():
        return False, "본문이 비어 있습니다 (자바스크립트로 그리는 페이지일 수 있습니다)."

    truncated = len(text) > max_chars
    return True, (f"[{resp.url}]\n" + text[:max_chars]
                  + ("\n…(이하 생략)" if truncated else ""))
