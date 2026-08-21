"""도메인 모델. 저장소(DB/메모리)와 무관한 순수 dataclass."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ts() -> float:
    return time.time()


class MemberType(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class ReplyMode(str, Enum):
    """GenTeam의 reply mode. 기본값이 MENTION인 것이 폭주 방지의 1차 방어선."""

    MENTION = "mention"   # @멘션 됐을 때만 반응 (기본값)
    ALL = "all"           # 채널의 모든 메시지에 반응


class MessageKind(str, Enum):
    CHAT = "chat"           # 사람/에이전트의 일반 발화
    TOOL_LOG = "tool_log"   # 에이전트의 내부 툴 실행 기록 (UI에서 접어둠)
    SYSTEM = "system"       # 입장/퇴장/상태 변경 등


class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


class RunStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Agent:
    id: str
    workspace_id: str
    name: str                      # @멘션에 쓰이는 핸들. 워크스페이스 내 유일해야 함
    role_prompt: str               # 역할 기술서(JD). 시스템 프롬프트의 핵심
    model: Optional[str] = None    # None이면 기본 모델
    reply_mode: ReplyMode = ReplyMode.MENTION
    enabled: bool = True
    avatar: Optional[str] = None
    max_steps: int = 8             # 한 턴에서 허용할 툴 루프 최대 횟수
    created_at: float = field(default_factory=now_ts)


@dataclass
class Human:
    id: str
    workspace_id: str
    name: str
    email: Optional[str] = None


@dataclass
class Channel:
    id: str
    workspace_id: str
    name: str
    is_dm: bool = False
    topic: str = ""
    created_at: float = field(default_factory=now_ts)


@dataclass
class ChannelMember:
    channel_id: str
    member_type: MemberType
    member_id: str
    # 채널별 reply mode 오버라이드. None이면 에이전트 기본값 사용
    reply_mode: Optional[ReplyMode] = None


@dataclass
class Message:
    id: str
    channel_id: str
    author_type: MemberType
    author_id: str
    author_name: str
    text: str
    kind: MessageKind = MessageKind.CHAT
    thread_id: Optional[str] = None   # None이면 채널 최상위 메시지
    # --- 폭주 제어용 계보 정보 ---
    trace_id: str = ""                # 사람의 한 마디에서 시작된 연쇄 전체의 ID
    depth: int = 0                    # 그 연쇄에서 몇 번째 홉인가
    caused_by: Optional[str] = None    # 이 메시지를 유발한 메시지 ID
    created_at: float = field(default_factory=now_ts)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    """Phase 3용. 스키마만 미리 잡아두어 나중에 마이그레이션이 없도록 함."""

    id: str
    channel_id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.TODO
    assignee_type: Optional[MemberType] = None
    assignee_id: Optional[str] = None
    thread_id: Optional[str] = None
    created_at: float = field(default_factory=now_ts)


@dataclass
class FileRecord:
    """채널에 올라온 파일.

    실제 바이트는 파일시스템에 file_id 이름으로 저장한다. 사용자가 준
    파일명을 경로로 쓰지 않는다 — '../../etc/passwd' 같은 이름이 그대로
    경로가 되면 끝이다.
    """

    id: str
    channel_id: str
    name: str                      # 원본 파일명 (표시용)
    size: int
    content_type: str
    uploader_type: MemberType = MemberType.HUMAN
    uploader_id: str = ""
    uploader_name: str = ""
    #: 이 파일을 알린 채널 메시지
    message_id: Optional[str] = None
    created_at: float = field(default_factory=now_ts)

    @property
    def is_text(self) -> bool:
        ct = (self.content_type or "").lower()
        return (ct.startswith("text/") or "json" in ct or "csv" in ct
                or "xml" in ct or "yaml" in ct or ct in ("", "application/octet-stream"))


@dataclass
class ChannelSummary:
    """채널의 롤링 요약.

    최근 N개만 원문으로 넣고 그 이전은 이 요약으로 대체한다.
    증분 생성이 핵심 — 매번 전체를 다시 읽으면 요약 비용이 채널 길이에
    비례해 커져서 애초 문제를 해결하지 못한다.
    """

    id: str
    channel_id: str
    text: str
    #: 이 메시지까지 요약에 포함됐다 (다음 증분의 시작점)
    up_to_message_id: str
    up_to_created_at: float
    #: 요약이 대체하는 원본 메시지 수 (관측용)
    covered_count: int = 0
    created_at: float = field(default_factory=now_ts)


@dataclass
class AgentRun:
    """에이전트 한 턴의 실행 기록. 멱등성 키이자 관측/과금 단위."""

    id: str
    agent_id: str
    channel_id: str
    trigger_message_id: str
    trace_id: str
    depth: int
    status: RunStatus = RunStatus.RUNNING
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None
    created_at: float = field(default_factory=now_ts)

    @property
    def idem_key(self) -> str:
        return f"{self.agent_id}:{self.trigger_message_id}"
