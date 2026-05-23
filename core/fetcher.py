from __future__ import annotations

import asyncio
import importlib
from typing import Any

from astrbot.api import logger

from .parser import parse_dynamic_item


class BilibiliFetchError(RuntimeError):
    """Raised when Bilibili dynamic fetching fails."""


class BilibiliFetcher:
    def __init__(
        self,
        page_limit: int = 1,
        empty_retry_count: int = 2,
    ) -> None:
        self.page_limit = max(1, page_limit)
        self.empty_retry_count = max(0, empty_retry_count)

    async def fetch_user_dynamics(self, uid: int) -> list[dict[str, Any]]:
        user_module = self._import_bilibili_user_module()
        bilibili_user = user_module.User(uid)

        items: list[dict[str, Any]] = []
        offset = ""
        for page_index in range(self.page_limit):
            data = await self._fetch_dynamic_page(
                bilibili_user,
                uid=uid,
                offset=offset,
                retry_empty=page_index == 0,
            )

            if not isinstance(data, dict):
                raise BilibiliFetchError(
                    f"获取 UID {uid} 动态返回异常：{type(data).__name__}"
                )

            page_items = data.get("items") or []
            if isinstance(page_items, list):
                items.extend(item for item in page_items if isinstance(item, dict))

            if not data.get("has_more"):
                break
            offset = str(data.get("offset") or "")
            if not offset:
                break
            if page_index + 1 < self.page_limit:
                await asyncio.sleep(1)

        return items

    async def _fetch_dynamic_page(
        self,
        bilibili_user,
        *,
        uid: int,
        offset: str,
        retry_empty: bool,
    ) -> dict[str, Any]:
        max_attempts = 1 + (self.empty_retry_count if retry_empty and not offset else 0)
        last_data: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                data = await bilibili_user.get_dynamics_new(offset=offset)
            except Exception as exc:  # noqa: BLE001
                raise BilibiliFetchError(f"获取 UID {uid} 动态失败：{exc}") from exc

            if not isinstance(data, dict):
                raise BilibiliFetchError(
                    f"获取 UID {uid} 动态返回异常：{type(data).__name__}"
                )

            last_data = data
            page_items = data.get("items") or []
            if page_items or attempt >= max_attempts:
                return data

            logger.info(
                "[BilibiliPosts] UID %s 首次动态列表为空，%s/%s 次重试。",
                uid,
                attempt,
                max_attempts - 1,
            )
            await asyncio.sleep(1)

        return last_data or {}

    async def fetch_and_parse(self, uid: int):
        raw_items = await self.fetch_user_dynamics(uid)
        parsed = []
        for raw in raw_items:
            item = parse_dynamic_item(raw)
            if item is None:
                logger.warning("[BilibiliPosts] UID %s 存在无法解析的动态。", uid)
                continue
            parsed.append(item)
        return parsed

    @staticmethod
    def _import_bilibili_user_module():
        try:
            return importlib.import_module("bilibili_api.user")
        except ImportError as exc:
            raise BilibiliFetchError(
                "缺少依赖 bilibili-api-python，请先安装 requirements.txt。"
            ) from exc
