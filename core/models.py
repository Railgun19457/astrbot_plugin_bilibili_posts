from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class DynamicKind(StrEnum):
    VIDEO = "video"
    IMAGE = "image"
    WORD = "word"
    FORWARD = "forward"
    OTHER = "other"


DYNAMIC_KIND_LABELS: dict[DynamicKind, str] = {
    DynamicKind.VIDEO: "视频投稿",
    DynamicKind.IMAGE: "图文/图片",
    DynamicKind.WORD: "纯文字",
    DynamicKind.FORWARD: "转发",
    DynamicKind.OTHER: "其他",
}

DYNAMIC_KIND_ALIASES: dict[str, DynamicKind] = {
    "video": DynamicKind.VIDEO,
    "视频": DynamicKind.VIDEO,
    "视频投稿": DynamicKind.VIDEO,
    "av": DynamicKind.VIDEO,
    "image": DynamicKind.IMAGE,
    "图片": DynamicKind.IMAGE,
    "图文": DynamicKind.IMAGE,
    "图文/图片": DynamicKind.IMAGE,
    "draw": DynamicKind.IMAGE,
    "word": DynamicKind.WORD,
    "文字": DynamicKind.WORD,
    "纯文字": DynamicKind.WORD,
    "forward": DynamicKind.FORWARD,
    "转发": DynamicKind.FORWARD,
    "other": DynamicKind.OTHER,
    "其他": DynamicKind.OTHER,
}


class ForwardOption(StrEnum):
    RENDER_IMAGE = "render_image"
    ORIGINAL_LINK = "original_link"


FORWARD_OPTION_ALIASES: dict[str, ForwardOption] = {
    "render_image": ForwardOption.RENDER_IMAGE,
    "渲染图": ForwardOption.RENDER_IMAGE,
    "概览图": ForwardOption.RENDER_IMAGE,
    "original_link": ForwardOption.ORIGINAL_LINK,
    "原始链接": ForwardOption.ORIGINAL_LINK,
}


@dataclass(frozen=True)
class MonitorUser:
    uid: int
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or str(self.uid)


@dataclass(frozen=True)
class MonitorTemplate:
    index: int
    name: str
    enabled: bool
    users: tuple[MonitorUser, ...]
    dynamic_kinds: frozenset[DynamicKind]
    forward_options: frozenset[ForwardOption]
    session_umos: tuple[str, ...]

    @property
    def state_key_prefix(self) -> str:
        safe_name = self.name.strip() or f"template-{self.index}"
        return f"{self.index}:{safe_name}"


@dataclass(frozen=True)
class PluginConfig:
    enable_auto_check: bool
    check_interval_minutes: int
    request_timeout_seconds: int
    page_limit: int
    default_command_dynamic_count: int
    templates: tuple[MonitorTemplate, ...]


@dataclass(frozen=True)
class DynamicAuthor:
    uid: int | None = None
    name: str = ""
    face: str = ""
    pub_ts: int | None = None


@dataclass(frozen=True)
class DynamicVideo:
    aid: str = ""
    bvid: str = ""
    title: str = ""
    desc: str = ""
    url: str = ""
    cover: str = ""
    duration: str = ""


@dataclass(frozen=True)
class DynamicStats:
    like: int | None = None
    comment: int | None = None
    forward: int | None = None


@dataclass(frozen=True)
class DynamicItem:
    id: str
    kind: DynamicKind
    raw_type: str = ""
    text: str = ""
    author: DynamicAuthor = field(default_factory=DynamicAuthor)
    images: tuple[str, ...] = ()
    video: DynamicVideo | None = None
    url: str = ""
    publish_time: int | None = None
    orig: DynamicItem | None = None
    stats: DynamicStats = field(default_factory=DynamicStats)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def title(self) -> str:
        if self.video and self.video.title:
            return self.video.title
        return self.text.splitlines()[0][:80] if self.text else "新动态"


@dataclass(frozen=True)
class RenderResult:
    image_path: Path | None
    text: str
    link: str
