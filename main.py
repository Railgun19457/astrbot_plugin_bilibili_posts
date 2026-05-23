from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools

from .core.config import load_plugin_config
from .core.fetcher import BilibiliFetcher
from .core.models import (
    DYNAMIC_KIND_LABELS,
    DynamicItem,
    ForwardOption,
    MonitorTemplate,
    MonitorUser,
)
from .core.renderer import DynamicRenderer
from .core.state import DynamicStateStore

PLUGIN_NAME = "astrbot_plugin_bilibili_posts"


class BilibiliPostsPlugin(Star):
    """定时检测 B站用户动态并推送到指定会话。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.raw_config = config or {}
        self.config = load_plugin_config(self.raw_config)
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.temp_dir = self.data_dir / "temp"
        self.cache_dir = self.data_dir / "cache"
        self.state = DynamicStateStore(self.data_dir / "state.json")
        self.renderer = DynamicRenderer(
            self.temp_dir,
            self.cache_dir,
            timeout=self.config.request_timeout_seconds,
        )
        self._monitor_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._schedule_event = asyncio.Event()
        self._next_auto_check_at: float | None = None
        self._last_summary = "尚未执行检测。"
        self._last_error = ""

    @filter.command("哔哩状态")
    async def status_command(self, event: AstrMessageEvent):
        """查看 B站动态检测插件状态。"""

        self._reload_config()
        enabled_templates = self._enabled_templates()
        uid_count = sum(len(template.users) for template in enabled_templates)
        next_check = self._format_next_check()
        lines = [
            "B站动态检测状态",
            f"自动检测：{'启用' if self.config.enable_auto_check else '禁用'}",
            f"检测频率：{self.config.check_interval_minutes} 分钟",
            f"指令默认动态数量：{self.config.default_command_dynamic_count} 条",
            f"启用模板：{len(enabled_templates)} 个",
            f"检测 UID：{uid_count} 个",
            f"状态条目：{self.state.count_entries()} 个",
            f"下次检测：{next_check}",
            f"最近结果：{self._last_summary}",
        ]
        if self._last_error:
            lines.append(f"最近错误：{self._last_error}")
        yield event.plain_result("\n".join(lines))

    @filter.command("哔哩动态")
    async def dynamics_command(self, event: AstrMessageEvent):
        """推送指定数量的最新 B站动态。"""

        self._reload_config()
        count = self._parse_dynamic_count(
            self._extract_command_arg(event.message_str),
            default=self.config.default_command_dynamic_count,
        )
        summary = await self._push_latest_dynamics(count)
        if self.config.enable_auto_check:
            self._update_next_auto_check_time(notify=True)
        yield event.plain_result(summary or "动态推送完成。")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._ensure_monitor_task()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        if getattr(metadata, "module_path", None) == self.__module__:
            self._ensure_monitor_task()

    async def terminate(self) -> None:
        self._stop_event.set()
        self._schedule_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        try:
            self.state.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BilibiliPosts] 保存状态失败：%s", exc)
        logger.info("[BilibiliPosts] 插件已卸载。")

    def _ensure_monitor_task(self) -> None:
        self._reload_config()
        if self._monitor_task and not self._monitor_task.done():
            return
        if not self.config.enable_auto_check:
            logger.info("[BilibiliPosts] 自动检测未启用。")
            return
        self._stop_event.clear()
        self._update_next_auto_check_time(notify=False, initial=True)
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "[BilibiliPosts] 自动检测已启动，间隔 %s 分钟。",
            self.config.check_interval_minutes,
        )

    async def _monitor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._next_auto_check_at is None:
                    self._update_next_auto_check_time(notify=False)
                    continue

                wait_seconds = self._next_auto_check_at - now
                if wait_seconds > 0:
                    await self._wait_for_schedule_change(wait_seconds)
                    continue

                await self._run_check()
                self._update_next_auto_check_time(notify=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.exception("[BilibiliPosts] 自动检测任务异常退出。")

    async def _wait_for_schedule_change(self, timeout: float) -> None:
        wait_tasks = {
            asyncio.create_task(self._stop_event.wait()),
            asyncio.create_task(self._schedule_event.wait()),
        }
        try:
            done, pending = await asyncio.wait(
                wait_tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done and self._schedule_event.is_set():
                self._schedule_event.clear()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()

    def _update_next_auto_check_time(
        self, *, notify: bool, initial: bool = False
    ) -> None:
        delay = 5 if initial else max(60, self.config.check_interval_minutes * 60)
        self._next_auto_check_at = time.monotonic() + delay
        if notify:
            self._schedule_event.set()

    async def _run_check(self) -> str:
        self._reload_config()
        await self.renderer.cleanup_temp()

        enabled_templates = self._enabled_templates()
        if not enabled_templates:
            message = "未配置有效动态检测模板，已跳过检测。"
            self._last_summary = message
            logger.warning("[BilibiliPosts] %s", message)
            return message

        fetcher = BilibiliFetcher(
            page_limit=self.config.page_limit,
        )
        fetched = 0
        pushed = 0
        baselined = 0
        skipped = 0
        errors: list[str] = []

        for template in enabled_templates:
            for user in template.users:
                try:
                    result = await self._check_user_template(fetcher, template, user)
                except Exception as exc:  # noqa: BLE001
                    error = f"模板 {template.name} / UID {user.uid} 检测失败：{exc}"
                    logger.warning("[BilibiliPosts] %s", error)
                    errors.append(error)
                    continue

                fetched += result["fetched"]
                pushed += result["pushed"]
                baselined += result["baselined"]
                skipped += result["skipped"]
                await asyncio.sleep(1)

        self._save_state(errors)

        summary = (
            f"检测完成：抓取 {fetched} 条，推送 {pushed} 条，"
            f"建立基线 {baselined} 条，跳过 {skipped} 条，错误 {len(errors)} 个。"
        )
        self._last_summary = summary
        self._last_error = errors[0] if errors else ""
        return summary

    async def _push_latest_dynamics(self, count: int) -> str:
        await self.renderer.cleanup_temp()

        enabled_templates = self._enabled_templates()
        if not enabled_templates:
            message = "未配置有效动态检测模板，已跳过推送。"
            self._last_summary = message
            logger.warning("[BilibiliPosts] %s", message)
            return message

        fetcher = BilibiliFetcher(
            page_limit=max(self.config.page_limit, self._page_limit_for_count(count)),
        )
        fetched = 0
        pushed = 0
        skipped = 0
        errors: list[str] = []

        for template in enabled_templates:
            for user in template.users:
                try:
                    result = await self._push_latest_user_dynamics(
                        fetcher, template, user, count
                    )
                except Exception as exc:  # noqa: BLE001
                    error = (
                        f"模板 {template.name} / UID {user.uid} 推送最新动态失败：{exc}"
                    )
                    logger.warning("[BilibiliPosts] %s", error)
                    errors.append(error)
                    continue

                fetched += result["fetched"]
                pushed += result["pushed"]
                skipped += result["skipped"]
                await asyncio.sleep(1)

        self._save_state(errors)

        summary = (
            f"动态推送完成：每个 UID 请求最新 {count} 条，抓取 {fetched} 条，"
            f"推送 {pushed} 条，跳过 {skipped} 条，错误 {len(errors)} 个。"
        )
        self._last_summary = summary
        self._last_error = errors[0] if errors else ""
        if errors:
            return summary + "\n" + "\n".join(errors[:3])
        return summary

    async def _push_latest_user_dynamics(
        self,
        fetcher: BilibiliFetcher,
        template: MonitorTemplate,
        user: MonitorUser,
        count: int,
    ) -> dict[str, int]:
        state_key = self._state_key(template, user.uid)
        dynamics = await fetcher.fetch_and_parse(user.uid)

        filtered = self._filter_dynamics(dynamics, template)
        latest = sorted(
            filtered, key=lambda item: item.publish_time or 0, reverse=True
        )[:count]
        push_items = sorted(latest, key=lambda item: item.publish_time or 0)

        pushed = 0
        for dynamic in push_items:
            if await self._push_dynamic(template, dynamic):
                self.state.mark_seen(state_key, dynamic.id, dynamic.publish_time)
                pushed += 1

        if pushed > 0 and filtered:
            self.state.mark_many_seen(state_key, [dynamic.id for dynamic in filtered])
        elif not filtered:
            self.state.mark_initialized(state_key)

        return {
            "fetched": len(dynamics),
            "pushed": pushed,
            "skipped": len(dynamics) - len(latest),
        }

    async def _check_user_template(
        self,
        fetcher: BilibiliFetcher,
        template: MonitorTemplate,
        user: MonitorUser,
    ) -> dict[str, int]:
        state_key = self._state_key(template, user.uid)
        dynamics = await fetcher.fetch_and_parse(user.uid)

        filtered = self._filter_dynamics(dynamics, template)
        filtered.sort(key=lambda item: item.publish_time or 0)

        if not self.state.is_initialized(state_key):
            return await self._handle_first_run(state_key, filtered, template)

        pushed = 0
        skipped = len(dynamics) - len(filtered)
        for dynamic in filtered:
            if self.state.has_seen(state_key, dynamic.id):
                skipped += 1
                continue
            if await self._push_dynamic(template, dynamic):
                self.state.mark_seen(state_key, dynamic.id, dynamic.publish_time)
                pushed += 1

        return {
            "fetched": len(dynamics),
            "pushed": pushed,
            "baselined": 0,
            "skipped": skipped,
        }

    async def _handle_first_run(
        self,
        state_key: str,
        filtered: list[DynamicItem],
        template: MonitorTemplate,
    ) -> dict[str, int]:
        self.state.mark_many_seen(state_key, [dynamic.id for dynamic in filtered])
        logger.info(
            "[BilibiliPosts] 模板 %s 首次运行，仅建立 %s 条动态基线。",
            template.name,
            len(filtered),
        )
        self.state.mark_initialized(state_key)
        return {
            "fetched": len(filtered),
            "pushed": 0,
            "baselined": len(filtered),
            "skipped": 0,
        }

    async def _push_dynamic(
        self, template: MonitorTemplate, dynamic: DynamicItem
    ) -> bool:
        chains = await self._build_message_chains(template, dynamic)
        sent_targets = 0
        for umo in template.session_umos:
            if await self._send_message_chains(umo, chains):
                sent_targets += 1
        return sent_targets > 0

    async def _send_message_chains(self, umo: str, chains: list[MessageChain]) -> bool:
        if not chains:
            return False
        for chain in chains:
            try:
                if not await self.context.send_message(umo, chain):
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.warning("[BilibiliPosts] 发送到 UMO %s 失败：%s", umo, exc)
                return False
        return True

    async def _build_message_chains(
        self, template: MonitorTemplate, dynamic: DynamicItem
    ) -> list[MessageChain]:
        chains: list[MessageChain] = []
        image_path: Path | None = None
        if ForwardOption.RENDER_IMAGE in template.forward_options:
            try:
                image_path = await self.renderer.render(dynamic, template)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[BilibiliPosts] 渲染动态 %s 失败：%s", dynamic.id, exc)

        if image_path:
            chains.append(MessageChain([Image.fromFileSystem(str(image_path))]))

        text = self._format_text_message(template, dynamic)
        if not chains and not text:
            text = self._format_text_message(template, dynamic, include_summary=True)
        if text:
            chains.append(MessageChain([Plain(text)]))

        return chains

    def _format_text_message(
        self,
        template: MonitorTemplate,
        dynamic: DynamicItem,
        *,
        include_summary: bool = False,
    ) -> str:
        parts: list[str] = []
        if include_summary:
            author = dynamic.author.name or "Bilibili 用户"
            kind_label = DYNAMIC_KIND_LABELS.get(dynamic.kind, "动态")
            title = dynamic.title or "新动态"
            parts.append(f"{author} 发布了{kind_label}：{title}")
            if dynamic.orig:
                orig_author = dynamic.orig.author.name or "原作者"
                parts.append(f"转发自 @{orig_author}：{dynamic.orig.title}")

        if ForwardOption.ORIGINAL_LINK in template.forward_options and dynamic.url:
            parts.append(dynamic.url)
        if (
            dynamic.orig
            and ForwardOption.ORIGINAL_LINK in template.forward_options
            and dynamic.orig.url
        ):
            parts.append(f"原动态：{dynamic.orig.url}")
        return "\n".join(part for part in parts if part)

    def _reload_config(self) -> None:
        self.config = load_plugin_config(self.raw_config)
        self.renderer.timeout = self.config.request_timeout_seconds

    def _enabled_templates(self) -> list[MonitorTemplate]:
        return [template for template in self.config.templates if template.enabled]

    def _save_state(self, errors: list[str]) -> None:
        try:
            self.state.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BilibiliPosts] 保存状态失败：%s", exc)
            errors.append(f"保存状态失败：{exc}")

    @staticmethod
    def _filter_dynamics(
        dynamics: list[DynamicItem], template: MonitorTemplate
    ) -> list[DynamicItem]:
        return [
            dynamic for dynamic in dynamics if dynamic.kind in template.dynamic_kinds
        ]

    @staticmethod
    def _state_key(template: MonitorTemplate, uid: int) -> str:
        return f"{template.state_key_prefix}:uid:{uid}"

    def _format_next_check(self) -> str:
        if self._next_auto_check_at is None:
            return "未计划"
        remaining = max(0, int(self._next_auto_check_at - time.monotonic()))
        if remaining < 60:
            return f"{remaining} 秒后"
        return f"约 {remaining // 60} 分钟后"

    @staticmethod
    def _parse_dynamic_count(argument: str, *, default: int) -> int:
        try:
            count = int((argument or "").strip() or default)
        except ValueError:
            count = default
        return max(1, min(20, count))

    @staticmethod
    def _page_limit_for_count(count: int) -> int:
        return max(1, min(5, (count + 11) // 12))

    @staticmethod
    def _extract_command_arg(message: str) -> str:
        parts = (message or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
