"""파일 저장과 읽기.

바이트는 파일시스템에 두고 메타데이터만 DB 에 넣는다.

보안 요점 — **사용자가 준 파일명을 경로로 쓰지 않는다.**
'../../etc/passwd' 같은 이름이 그대로 경로가 되면 끝이다. 디스크에는
생성한 file_id 로만 저장하고, 원본 이름은 표시용 메타데이터로만 남긴다.

에이전트는 file_id 로만 접근하고, 툴이 채널 소속을 확인하므로 다른 채널의
파일을 읽을 수 없다.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

#: 업로드 상한. 이걸 넘으면 컨텍스트에 넣을 수도 없고 디스크만 먹는다.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: 한 번에 읽어 돌려줄 최대 글자 수 (프롬프트 보호)
DEFAULT_READ_CHARS = 4000
MAX_READ_CHARS = 20000
#: describe_table 이 훑을 최대 행 수. 큰 파일에서 전수 스캔을 막는다.
MAX_SCAN_ROWS = 50_000

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def data_dir() -> Path:
    d = Path(os.getenv("FILE_STORE_DIR", "data/files")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(file_id: str) -> Path:
    """file_id 로만 경로를 만든다. 형식이 어긋나면 거부한다."""
    if not _ID_RE.match(file_id or ""):
        raise ValueError(f"올바르지 않은 file_id: {file_id!r}")
    return data_dir() / file_id


def save_bytes(file_id: str, data: bytes) -> int:
    p = path_for(file_id)
    p.write_bytes(data)
    return len(data)


def delete_bytes(file_id: str) -> None:
    try:
        path_for(file_id).unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _decode(raw: bytes, *, partial: bool = False) -> str:
    """바이트를 텍스트로. partial=True 면 잘린 멀티바이트 꼬리를 버린다.

    주의: cp949/latin-1 은 거의 어떤 바이트열도 받아들인다. 그래서 UTF-8
    파일을 문자 경계에서 자른 조각을 그냥 폴백에 넘기면 예외 없이 깨진
    글자가 나온다. 증분 디코더로 불완전한 꼬리를 먼저 버려야 한다.
    """
    if partial:
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")()
        try:
            out = dec.decode(raw, False)   # 미완성 시퀀스는 버퍼에 남겨둔다
            if out:
                return out
        except UnicodeDecodeError:
            pass   # UTF-8 이 아니다 → 아래 폴백으로

    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(file_id: str, *, max_chars: int = DEFAULT_READ_CHARS,
              offset: int = 0) -> Tuple[bool, str]:
    """텍스트로 읽는다. 큰 파일은 offset 으로 이어 읽을 수 있다."""
    max_chars = max(200, min(int(max_chars or DEFAULT_READ_CHARS), MAX_READ_CHARS))
    try:
        p = path_for(file_id)
    except ValueError as exc:
        return False, str(exc)
    if not p.exists():
        return False, "파일 본문을 찾을 수 없습니다."

    # 필요한 만큼만 읽는다 (문자 하나가 최대 4바이트)
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        total = fh.tell()
        fh.seek(0)
        want = min(total, (offset + max_chars) * 4 + 4096)
        raw = fh.read(want)
    # 파일 전체를 읽었으면 잘린 꼬리가 없다
    text = _decode(raw, partial=(want < total))
    chunk = text[offset:offset + max_chars]
    if not chunk:
        return True, "(더 읽을 내용이 없습니다)"
    more = len(text) > offset + max_chars or len(raw) < total
    tail = (f"\n…(계속 읽으려면 offset={offset + max_chars})" if more else "")
    return True, chunk + tail


def _sniff(sample: str) -> Optional[str]:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        # 스니퍼가 실패해도 흔한 구분자 중 가장 많이 나온 것을 쓴다
        counts = {d: sample.count(d) for d in (",", "\t", ";", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else None


def describe_table(file_id: str, *, sample_rows: int = 5) -> Tuple[bool, str]:
    """CSV/TSV 의 구조를 파악한다.

    데이터분석가에게 필요한 건 원문 4,000자가 아니라 **컬럼이 뭐고 몇 행이고
    각 컬럼에 어떤 값이 들어있는가** 다. 원문만 주면 앞부분 몇 줄만 보고
    전체를 추측하게 된다.
    """
    try:
        p = path_for(file_id)
    except ValueError as exc:
        return False, str(exc)
    if not p.exists():
        return False, "파일 본문을 찾을 수 없습니다."

    text = _decode(p.read_bytes())
    if not text.strip():
        return False, "파일이 비어 있습니다."

    delim = _sniff(text[:8192])
    if delim is None:
        return False, "구분자를 찾지 못했습니다. CSV/TSV 가 아니면 read_file 을 쓰세요."

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        return False, "헤더를 읽지 못했습니다."

    cols = [c.strip() or f"col{i}" for i, c in enumerate(header)]
    n = len(cols)
    stats = [{"empty": 0, "num": 0, "vals": set(), "min": None, "max": None}
             for _ in range(n)]
    head: List[List[str]] = []
    rows = 0
    truncated = False

    for row in reader:
        rows += 1
        if rows > MAX_SCAN_ROWS:
            truncated = True
            break
        if len(head) < sample_rows:
            head.append(row)
        for i in range(min(n, len(row))):
            v = row[i].strip()
            st = stats[i]
            if not v:
                st["empty"] += 1
                continue
            if len(st["vals"]) < 200:
                st["vals"].add(v)
            try:
                f = float(v.replace(",", ""))
                st["num"] += 1
                st["min"] = f if st["min"] is None else min(st["min"], f)
                st["max"] = f if st["max"] is None else max(st["max"], f)
            except ValueError:
                pass

    lines = [f"구분자: {'TAB' if delim == chr(9) else repr(delim)} · "
             f"{rows:,}행{' 이상 (스캔 상한 도달)' if truncated else ''} · {n}개 컬럼", "",
             "컬럼:"]
    for i, c in enumerate(cols):
        st = stats[i]
        seen = rows - st["empty"]
        kind = "숫자" if seen and st["num"] >= seen * 0.9 else "문자"
        detail = f"{kind}"
        if kind == "숫자" and st["min"] is not None:
            detail += f" {st['min']:g}~{st['max']:g}"
        else:
            uniq = len(st["vals"])
            sample = ", ".join(list(st["vals"])[:3])
            detail += f" 고유{uniq}{'+' if uniq >= 200 else ''}개"
            if sample:
                detail += f" (예: {sample[:60]})"
        if st["empty"]:
            detail += f" · 빈값 {st['empty']:,}"
        lines.append(f"  {i}. {c} — {detail}")

    lines += ["", f"처음 {len(head)}행:"]
    lines.append("  " + " | ".join(cols))
    for r in head:
        lines.append("  " + " | ".join(x[:24] for x in r))
    return True, "\n".join(lines)


def preview_for_message(name: str, content_type: str, raw: bytes) -> str:
    """업로드 시 채널 메시지에 붙일 한 줄 요약."""
    ct = (content_type or "").lower()
    if "csv" in ct or name.lower().endswith((".csv", ".tsv")):
        text = _decode(raw[:200_000])
        lines = text.splitlines()
        if lines:
            delim = _sniff(text[:8192]) or ","
            ncol = len(lines[0].split(delim))
            total = _decode(raw).count("\n")
            return f"{total:,}행 · {ncol}개 컬럼"
    if "json" in ct or name.lower().endswith(".json"):
        try:
            obj = json.loads(_decode(raw))
            if isinstance(obj, list):
                return f"JSON 배열 · {len(obj):,}개 항목"
            if isinstance(obj, dict):
                return f"JSON 객체 · 키 {len(obj)}개"
        except (ValueError, TypeError):
            pass
    if raw[:1] and _looks_text(raw):
        return f"{_decode(raw).count(chr(10)) + 1:,}줄"
    return ""


def _looks_text(raw: bytes) -> bool:
    chunk = raw[:4096]
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return True   # cp949 등일 수 있다 — 바이너리 판정은 NUL 로만 한다
