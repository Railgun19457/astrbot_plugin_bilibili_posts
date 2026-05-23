from __future__ import annotations

from typing import Any

from .models import DynamicAuthor, DynamicItem, DynamicKind, DynamicStats, DynamicVideo

MAX_FORWARD_DEPTH = 1


def normalize_url(url: Any) -> str:
    if not url:
        return ""
    text = str(url).strip()
    if text.startswith("//"):
        return "https:" + text
    return text


def rich_text_to_plain_text(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    return "".join(
        _rich_text_node_to_text(node) for node in nodes if isinstance(node, dict)
    )


def _rich_text_node_to_text(node: dict[str, Any]) -> str:
    emoji = node.get("emoji")
    if isinstance(emoji, dict):
        text = str(emoji.get("text") or "")
        if text:
            return text
    return str(node.get("orig_text") or node.get("text") or "")


def parse_dynamic_item(item: dict[str, Any], *, depth: int = 0) -> DynamicItem | None:
    if not isinstance(item, dict):
        return None

    modules = item.get("modules") or {}
    module_dynamic = modules.get("module_dynamic") or {}
    desc = module_dynamic.get("desc") or {}
    major = module_dynamic.get("major") or {}
    author_raw = modules.get("module_author") or {}
    stat_raw = (
        modules.get("module_stat")
        or modules.get("module_interaction")
        or item.get("stat")
        or {}
    )

    dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
    raw_type = str(item.get("type") or "")
    major_type = str(major.get("type") or "")
    text = _extract_text(desc)
    images: list[str] = []
    video: DynamicVideo | None = None
    url = normalize_url(item.get("jump_url") or item.get("url"))

    kind = _detect_kind(raw_type, major_type)

    if major_type == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") or {}
        summary = opus.get("summary") or {}
        text = text or _extract_text(summary)
        images = [
            normalize_url(pic.get("url"))
            for pic in opus.get("pics", [])
            if pic.get("url")
        ]
        url = url or normalize_url(opus.get("jump_url"))
        if kind == DynamicKind.OTHER:
            kind = DynamicKind.IMAGE if images else DynamicKind.WORD

    if major_type == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") or {}
        video = DynamicVideo(
            title=str(archive.get("title") or ""),
            desc=str(
                archive.get("desc")
                or archive.get("desc_second")
                or archive.get("intro")
                or ""
            ),
            url=normalize_url(archive.get("jump_url")),
            cover=normalize_url(archive.get("cover")),
            duration=str(archive.get("duration_text") or ""),
        )
        text = text or video.title or video.desc
        url = video.url or url
        kind = DynamicKind.VIDEO

    orig = None
    if item.get("orig") and depth < MAX_FORWARD_DEPTH:
        orig = parse_dynamic_item(item["orig"], depth=depth + 1)
        kind = DynamicKind.FORWARD

    author = DynamicAuthor(
        uid=_safe_int(author_raw.get("mid")),
        name=str(author_raw.get("name") or ""),
        face=normalize_url(author_raw.get("face")),
    )
    publish_time = _safe_int(author_raw.get("pub_ts")) or _safe_int(item.get("pub_ts"))

    if not url and dynamic_id:
        url = f"https://t.bilibili.com/{dynamic_id}"

    if not dynamic_id:
        dynamic_id = _fallback_dynamic_id(raw_type, text, url)

    return DynamicItem(
        id=dynamic_id,
        kind=kind,
        text=text or "",
        author=author,
        images=tuple(img for img in images if img),
        video=video,
        url=url,
        publish_time=publish_time,
        orig=orig,
        stats=_parse_stats(stat_raw),
    )


def _detect_kind(raw_type: str, major_type: str) -> DynamicKind:
    if raw_type == "DYNAMIC_TYPE_FORWARD":
        return DynamicKind.FORWARD
    if raw_type == "DYNAMIC_TYPE_AV" or major_type == "MAJOR_TYPE_ARCHIVE":
        return DynamicKind.VIDEO
    if raw_type == "DYNAMIC_TYPE_DRAW":
        return DynamicKind.IMAGE
    if raw_type == "DYNAMIC_TYPE_WORD":
        return DynamicKind.WORD
    return DynamicKind.OTHER


def _extract_text(data: dict[str, Any]) -> str:
    text = str(data.get("text") or "").strip()
    if text:
        return text
    return rich_text_to_plain_text(data.get("rich_text_nodes")).strip()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_stats(data: Any) -> DynamicStats:
    if not isinstance(data, dict):
        return DynamicStats()
    like = data.get("like") or {}
    comment = data.get("comment") or {}
    forward = data.get("forward") or {}
    return DynamicStats(
        like=_stat_count(like, data, "like"),
        comment=_stat_count(comment, data, "comment", "reply"),
        forward=_stat_count(forward, data, "forward", "repost"),
    )


def _stat_count(value: Any, data: dict[str, Any], *keys: str) -> int | None:
    if isinstance(value, dict):
        for key in ("count", "num", "value"):
            parsed = _safe_int(value.get(key))
            if parsed is not None:
                return parsed
    parsed = _safe_int(value)
    if parsed is not None:
        return parsed
    for key in keys:
        for suffix in ("_count", "_num"):
            parsed = _safe_int(data.get(f"{key}{suffix}"))
            if parsed is not None:
                return parsed
    return None


def _fallback_dynamic_id(raw_type: str, text: str, url: str) -> str:
    seed = f"{raw_type}:{text}:{url}"
    return str(abs(hash(seed)))
