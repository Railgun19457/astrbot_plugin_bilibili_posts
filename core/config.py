from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from astrbot.api import AstrBotConfig, logger

from .models import (
    DYNAMIC_KIND_ALIASES,
    FORWARD_OPTION_ALIASES,
    DynamicKind,
    ForwardOption,
    MonitorTemplate,
    MonitorUser,
    PluginConfig,
)

TEMPLATE_KEY_FIELD = "__template_key"

DEFAULT_DYNAMIC_KINDS = frozenset(
    {
        DynamicKind.VIDEO,
        DynamicKind.IMAGE,
        DynamicKind.WORD,
        DynamicKind.FORWARD,
    }
)
DEFAULT_FORWARD_OPTIONS = frozenset(
    {
        ForwardOption.RENDER_IMAGE,
        ForwardOption.ORIGINAL_LINK,
    }
)


def load_plugin_config(
    config: AstrBotConfig | Mapping[str, Any] | None,
) -> PluginConfig:
    raw = _as_mapping(config)
    templates = tuple(
        template
        for template in (
            _parse_template(index, item)
            for index, item in enumerate(_as_list(raw.get("templates")), start=1)
        )
        if template is not None
    )

    return PluginConfig(
        enable_auto_check=_as_bool(raw.get("enable_auto_check"), default=True),
        check_interval_minutes=_clamp_int(
            raw.get("check_interval_minutes"), default=30, minimum=1, maximum=1440
        ),
        request_timeout_seconds=_clamp_int(
            raw.get("request_timeout_seconds"), default=20, minimum=5, maximum=120
        ),
        page_limit=_clamp_int(raw.get("page_limit"), default=1, minimum=1, maximum=5),
        default_command_dynamic_count=_clamp_int(
            raw.get("default_command_dynamic_count"),
            default=3,
            minimum=1,
            maximum=20,
        ),
        templates=templates,
    )


def _parse_template(index: int, value: Any) -> MonitorTemplate | None:
    raw = _as_mapping(value)
    name = _as_str(raw.get("name")) or f"动态模板 {index}"
    enabled = _as_bool(raw.get("enabled"), default=True)
    users = tuple(_parse_users(raw.get("users")))
    session_umos = tuple(
        item
        for item in (_as_str(umo) for umo in _as_list(raw.get("session_umos")))
        if item
    )

    if not users:
        logger.warning("[BilibiliPosts] 模板 %s 未配置有效 UID，已跳过。", name)
        return None
    if not session_umos:
        logger.warning("[BilibiliPosts] 模板 %s 未配置目标会话，已跳过。", name)
        return None

    return MonitorTemplate(
        index=index,
        name=name,
        enabled=enabled,
        users=users,
        dynamic_kinds=_parse_dynamic_kinds(raw.get("dynamic_types")),
        forward_options=_parse_forward_options(raw.get("forward_options")),
        session_umos=session_umos,
    )


def _parse_users(value: Any) -> Iterable[MonitorUser]:
    for item in _as_list(value):
        if isinstance(item, Mapping):
            uid_text = _as_str(item.get("uid"))
            name = _as_str(item.get("name") or item.get("display_name"))
        else:
            uid_text, name = _split_user_entry(_as_str(item))

        match = re.search(r"\d+", uid_text)
        if not match:
            logger.warning("[BilibiliPosts] 无效 UID 配置：%s", item)
            continue

        try:
            uid = int(match.group(0))
        except ValueError:
            logger.warning("[BilibiliPosts] UID 解析失败：%s", item)
            continue

        if uid <= 0:
            logger.warning("[BilibiliPosts] UID 必须大于 0：%s", item)
            continue
        yield MonitorUser(uid=uid, display_name=name)


def _split_user_entry(value: str) -> tuple[str, str]:
    for sep in (":", "：", "|", ","):
        if sep in value:
            left, right = value.split(sep, 1)
            return left.strip(), right.strip()
    return value.strip(), ""


def _parse_dynamic_kinds(value: Any) -> frozenset[DynamicKind]:
    kinds = {
        kind
        for kind in (_parse_dynamic_kind(item) for item in _as_list(value))
        if kind is not None
    }
    return frozenset(kinds) if kinds else DEFAULT_DYNAMIC_KINDS


def _parse_dynamic_kind(value: Any) -> DynamicKind | None:
    text = _as_str(value)
    if not text:
        return None
    return DYNAMIC_KIND_ALIASES.get(text) or DYNAMIC_KIND_ALIASES.get(text.lower())


def _parse_forward_options(value: Any) -> frozenset[ForwardOption]:
    if value is None or value == "":
        return DEFAULT_FORWARD_OPTIONS

    options = {
        option
        for option in (_parse_forward_option(item) for item in _as_list(value))
        if option is not None
    }
    return frozenset(options)


def _parse_forward_option(value: Any) -> ForwardOption | None:
    text = _as_str(value)
    if not text:
        return None
    return FORWARD_OPTION_ALIASES.get(text) or FORWARD_OPTION_ALIASES.get(text.lower())


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value is None or value == "":
        return []
    return [value]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "启用", "是"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "禁用", "否"}:
            return False
    return bool(value)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
