from __future__ import annotations

import asyncio
import hashlib
import textwrap
import time
from collections.abc import Iterable
from pathlib import Path

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .models import DynamicItem, MonitorTemplate

CANVAS_WIDTH = 960
PADDING = 42
BLOCK_GAP = 22
CARD_RADIUS = 28
BILI_PINK = (251, 114, 153)
TEXT_PRIMARY = (35, 38, 47)
TEXT_SECONDARY = (98, 104, 118)
TEXT_MUTED = (148, 154, 168)
BACKGROUND_TOP = (255, 246, 250)
BACKGROUND_BOTTOM = (244, 248, 255)
CARD_BG = (255, 255, 255)
BORDER = (232, 236, 245)
VIDEO_CARD_BG = (247, 249, 253)
EMOJI_FONT_CANDIDATES = [
    "C:/Windows/Fonts/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
]


class DynamicRenderer:
    def __init__(self, temp_dir: Path, cache_dir: Path, *, timeout: int = 20) -> None:
        self.temp_dir = temp_dir
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.font_regular = _load_font(28)
        self.font_medium = _load_font(32)
        self.font_large = _load_font(44)
        self.font_small = _load_font(22)
        self.font_tiny = _load_font(18)
        self.font_emoji_regular = _load_emoji_font(28)
        self.font_emoji_medium = _load_emoji_font(32)
        self.font_emoji_small = _load_emoji_font(22)

    async def render(self, dynamic: DynamicItem, template: MonitorTemplate) -> Path:
        return await asyncio.to_thread(self._render_sync, dynamic, template)

    async def cleanup_temp(self, *, older_than_seconds: int = 86400) -> None:
        cutoff = time.time() - older_than_seconds
        for path in self.temp_dir.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.debug("[BilibiliPosts] 清理临时图片失败：%s", path)

    def _render_sync(self, dynamic: DynamicItem, template: MonitorTemplate) -> Path:
        blocks = self._build_blocks(dynamic, template)
        gap_total = BLOCK_GAP * max(0, len(blocks) - 1)
        height = max(
            520,
            PADDING * 2 + sum(block["height"] for block in blocks) + gap_total,
        )
        image = Image.new("RGB", (CANVAS_WIDTH, height), BACKGROUND_BOTTOM)
        draw = ImageDraw.Draw(image)
        self._draw_gradient(image)
        self._rounded_rectangle(
            draw, (24, 24, CANVAS_WIDTH - 24, height - 24), CARD_RADIUS, CARD_BG
        )

        y = PADDING
        for block in blocks:
            draw_fn = block["draw"]
            draw_fn(draw, image, y)
            y += block["height"] + BLOCK_GAP

        output = self.temp_dir / f"bilibili_{dynamic.id}_{int(time.time())}.png"
        image.save(output, format="PNG", optimize=True)
        return output

    def _build_blocks(
        self, dynamic: DynamicItem, template: MonitorTemplate
    ) -> list[dict]:
        blocks = [
            {
                "height": 112,
                "draw": lambda draw, image, y: self._draw_header(
                    draw, image, y, dynamic
                ),
            },
        ]
        post_text = self._post_text(dynamic)
        if post_text:
            text_lines = self._wrap_text(
                post_text,
                self.font_regular,
                29,
                max_lines=5 if dynamic.video else 8,
            )
            blocks.append(
                {
                    "height": max(58, len(text_lines) * 38 + 12),
                    "draw": lambda draw, image, y: self._draw_text_lines(
                        draw, y, text_lines
                    ),
                }
            )

        media_block = self._media_block(dynamic)
        if media_block:
            blocks.append(media_block)

        if dynamic.orig:
            blocks.append(
                {
                    "height": 156,
                    "draw": lambda draw, image, y: self._draw_forward_card(
                        draw, y, dynamic.orig
                    ),
                }
            )

        stats = self._stats_items(dynamic)
        if stats:
            blocks.append(
                {
                    "height": 44,
                    "draw": lambda draw, image, y: self._draw_stats(draw, y, stats),
                }
            )

        blocks.append(
            {
                "height": 52,
                "draw": lambda draw, image, y: self._draw_footer(draw, y, dynamic),
            }
        )
        return blocks

    @staticmethod
    def _post_text(dynamic: DynamicItem) -> str:
        text = (dynamic.text or "").strip()
        if dynamic.video:
            title = (dynamic.video.title or "").strip()
            if text and _normalize_text(text) != _normalize_text(title):
                return text
            return ""
        return text or dynamic.title

    def _draw_header(
        self,
        draw: ImageDraw.ImageDraw,
        image: Image.Image,
        y: int,
        dynamic: DynamicItem,
    ) -> None:
        avatar = self._load_cached_image(dynamic.author.face, size=(76, 76))
        image.paste(avatar, (PADDING, y + 6), avatar if avatar.mode == "RGBA" else None)
        name = dynamic.author.name or "Bilibili 用户"
        self._draw_text(
            draw,
            (PADDING + 96, y + 6),
            name,
            self.font_medium,
            TEXT_PRIMARY,
            emoji_font=self.font_emoji_medium,
        )
        uid_text = (
            f"UID {dynamic.author.uid}" if dynamic.author.uid else "Bilibili Dynamic"
        )
        self._draw_text(
            draw,
            (PADDING + 96, y + 48),
            uid_text,
            self.font_small,
            TEXT_SECONDARY,
            emoji_font=self.font_emoji_small,
        )
        publish = _format_time(dynamic.publish_time)
        publish_width = _text_width(draw, publish, self.font_small)
        draw.text(
            (CANVAS_WIDTH - PADDING - publish_width, y + 18),
            publish,
            font=self.font_small,
            fill=TEXT_MUTED,
        )
        draw.line(
            (PADDING, y + 104, CANVAS_WIDTH - PADDING, y + 104), fill=BORDER, width=1
        )

    def _draw_text_lines(
        self, draw: ImageDraw.ImageDraw, y: int, lines: list[str]
    ) -> None:
        if not lines:
            lines = ["发布了一条新动态。"]
        for index, line in enumerate(lines):
            self._draw_text(
                draw,
                (PADDING, y + 8 + index * 38),
                line,
                self.font_regular,
                TEXT_PRIMARY,
                emoji_font=self.font_emoji_regular,
            )

    def _media_block(self, dynamic: DynamicItem) -> dict | None:
        if dynamic.video and dynamic.video.cover:
            title_lines = self._wrap_text(
                dynamic.video.title or dynamic.title,
                self.font_medium,
                19,
                max_lines=2,
            )
            desc_lines = self._wrap_text(
                dynamic.video.desc, self.font_small, 56, max_lines=4
            )
            desc_height = 42 + len(desc_lines) * 30 if desc_lines else 0
            height = 304 + desc_height
            return {
                "height": height,
                "draw": lambda draw, image, y: self._draw_video_card(
                    draw, image, y, dynamic, height, title_lines, desc_lines
                ),
            }
        if dynamic.images:
            rows = (
                1 if len(dynamic.images) <= 3 else 2 if len(dynamic.images) <= 6 else 3
            )
            return {
                "height": rows * 176 + (rows - 1) * 12,
                "draw": lambda draw, image, y: self._draw_images(
                    image, y, dynamic.images
                ),
            }
        return None

    def _draw_video_card(
        self,
        draw: ImageDraw.ImageDraw,
        image: Image.Image,
        y: int,
        dynamic: DynamicItem,
        height: int,
        title_lines: list[str],
        desc_lines: list[str],
    ) -> None:
        assert dynamic.video is not None
        x1 = PADDING
        x2 = CANVAS_WIDTH - PADDING
        self._rounded_rectangle(
            draw, (x1, y, x2, y + height), 24, VIDEO_CARD_BG, outline=BORDER
        )

        cover_x = x1 + 22
        cover_y = y + 22
        cover_width = 336
        cover_height = 210
        cover = self._load_cached_image(
            dynamic.video.cover, size=(cover_width, cover_height), rounded=18
        )
        image.paste(cover, (cover_x, cover_y), cover if cover.mode == "RGBA" else None)

        text_x = cover_x + cover_width + 28
        text_y = y + 22
        title_y = text_y + 8
        for index, line in enumerate(title_lines):
            self._draw_text(
                draw,
                (text_x, title_y + index * 38),
                line or "新视频",
                self.font_medium,
                TEXT_PRIMARY,
                emoji_font=self.font_emoji_medium,
            )

        info_y = title_y + max(1, len(title_lines)) * 38 + 18
        if dynamic.video.duration:
            duration_text = f"时长 {dynamic.video.duration}"
            duration_width = _text_width(draw, duration_text, self.font_small) + 34
            pill = (text_x, info_y, text_x + duration_width, info_y + 36)
            self._rounded_rectangle(draw, pill, 18, (255, 255, 255), outline=BORDER)
            draw.text(
                (text_x + 17, info_y + 6),
                duration_text,
                font=self.font_small,
                fill=TEXT_SECONDARY,
            )

        if desc_lines:
            desc_y = cover_y + cover_height + 22
            draw.line(
                (x1 + 22, desc_y - 16, x2 - 22, desc_y - 16), fill=BORDER, width=1
            )
            draw.text(
                (x1 + 22, desc_y),
                "简介",
                font=self.font_tiny,
                fill=TEXT_MUTED,
            )
            for index, line in enumerate(desc_lines):
                self._draw_text(
                    draw,
                    (x1 + 22, desc_y + 28 + index * 30),
                    line,
                    self.font_small,
                    TEXT_SECONDARY,
                    emoji_font=self.font_emoji_small,
                )

    def _draw_images(self, image: Image.Image, y: int, urls: Iterable[str]) -> None:
        urls = list(urls)[:9]
        size = 168
        gap = 12
        for index, url in enumerate(urls):
            row, col = divmod(index, 3)
            x = PADDING + col * (size + gap)
            item_y = y + row * (size + gap)
            thumb = self._load_cached_image(url, size=(size, size), rounded=16)
            image.paste(thumb, (x, item_y), thumb if thumb.mode == "RGBA" else None)

    def _draw_forward_card(
        self, draw: ImageDraw.ImageDraw, y: int, orig: DynamicItem
    ) -> None:
        x1 = PADDING
        x2 = CANVAS_WIDTH - PADDING
        self._rounded_rectangle(
            draw, (x1, y, x2, y + 144), 22, (247, 249, 253), outline=BORDER
        )
        draw.text((x1 + 24, y + 18), "转发原动态", font=self.font_small, fill=BILI_PINK)
        author = orig.author.name or "原作者"
        self._draw_text(
            draw,
            (x1 + 24, y + 52),
            f"@{author}",
            self.font_small,
            TEXT_SECONDARY,
            emoji_font=self.font_emoji_small,
        )
        summary = orig.title or orig.text or "原动态内容"
        lines = self._wrap_text(summary, self.font_small, 46, max_lines=2)
        for index, line in enumerate(lines):
            self._draw_text(
                draw,
                (x1 + 24, y + 84 + index * 28),
                line,
                self.font_small,
                TEXT_PRIMARY,
                emoji_font=self.font_emoji_small,
            )

    def _stats_items(self, dynamic: DynamicItem) -> list[tuple[str, str]]:
        return [
            ("👍", _format_count(dynamic.stats.like)),
            ("💬", _format_count(dynamic.stats.comment)),
            ("↗", _format_count(dynamic.stats.forward)),
        ]

    def _draw_stats(
        self, draw: ImageDraw.ImageDraw, y: int, items: list[tuple[str, str]]
    ) -> None:
        x = PADDING
        for icon, value in items:
            box_width = max(108, _text_width(draw, value, self.font_small) + 58)
            self._rounded_rectangle(
                draw,
                (x, y, x + box_width, y + 38),
                19,
                (247, 249, 253),
                outline=BORDER,
            )
            self._draw_text(
                draw,
                (x + 16, y + 7),
                icon,
                self.font_small,
                TEXT_MUTED,
                emoji_font=self.font_emoji_small,
            )
            draw.text((x + 46, y + 7), value, font=self.font_small, fill=TEXT_SECONDARY)
            x += box_width + 14

    def _draw_footer(
        self, draw: ImageDraw.ImageDraw, y: int, dynamic: DynamicItem
    ) -> None:
        draw.line((PADDING, y, CANVAS_WIDTH - PADDING, y), fill=BORDER, width=1)
        link_text = dynamic.url or "https://www.bilibili.com"
        draw.text(
            (PADDING, y + 18), "Bilibili 动态检测", font=self.font_tiny, fill=TEXT_MUTED
        )
        right_text = link_text[:52] + ("…" if len(link_text) > 52 else "")
        right_width = _text_width(draw, right_text, self.font_tiny)
        draw.text(
            (CANVAS_WIDTH - PADDING - right_width, y + 18),
            right_text,
            font=self.font_tiny,
            fill=TEXT_MUTED,
        )

    def _load_cached_image(
        self, url: str, *, size: tuple[int, int], rounded: int | None = None
    ) -> Image.Image:
        if not url:
            return _placeholder(size, rounded=rounded)
        cache_path = self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.img"
        if not cache_path.exists():
            try:
                data = _download_sync(url, self.timeout)
                cache_path.write_bytes(data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[BilibiliPosts] 图片下载失败 %s: %s", url, exc)
                return _placeholder(size, rounded=rounded)
        try:
            with Image.open(cache_path) as img:
                img = ImageOps.exif_transpose(img).convert("RGBA")
                img = ImageOps.fit(img, size, method=Image.Resampling.LANCZOS)
                if rounded:
                    img = _round_image(img, rounded)
                else:
                    img = _circle_image(img)
                return img
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BilibiliPosts] 缓存图片读取失败 %s: %s", cache_path, exc)
            return _placeholder(size, rounded=rounded)

    def _wrap_text(
        self, text: str, font: ImageFont.ImageFont, width_chars: int, *, max_lines: int
    ) -> list[str]:
        normalized = " ".join((text or "").split())
        if not normalized:
            return []
        rough_lines = textwrap.wrap(
            normalized, width=width_chars, replace_whitespace=False
        )
        lines = rough_lines[:max_lines]
        if len(rough_lines) > max_lines and lines:
            lines[-1] = lines[-1].rstrip("。,.，") + "…"
        return lines

    @staticmethod
    def _draw_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int],
        *,
        emoji_font: ImageFont.ImageFont | None = None,
    ) -> None:
        if not text:
            return
        x, y = xy
        for segment, is_emoji in _split_emoji_runs(text):
            active_font = emoji_font if is_emoji and emoji_font else font
            try:
                draw.text((x, y), segment, font=active_font, fill=fill)
            except (UnicodeEncodeError, OSError):
                fallback = segment.encode("ascii", "ignore").decode()
                draw.text((x, y), fallback, font=font, fill=fill)
                segment = fallback
                active_font = font
            x += _text_width(draw, segment, active_font)

    @staticmethod
    def _draw_gradient(image: Image.Image) -> None:
        draw = ImageDraw.Draw(image)
        width, height = image.size
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = tuple(
                int(BACKGROUND_TOP[i] * (1 - ratio) + BACKGROUND_BOTTOM[i] * ratio)
                for i in range(3)
            )
            draw.line((0, y, width, y), fill=color)

    @staticmethod
    def _rounded_rectangle(
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        radius: int,
        fill: tuple[int, int, int],
        outline: tuple[int, int, int] | None = None,
    ) -> None:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)


def _download_sync(url: str, timeout: int) -> bytes:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_emoji_font(size: int) -> ImageFont.ImageFont | None:
    for path in EMOJI_FONT_CANDIDATES:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return None


def _placeholder(size: tuple[int, int], *, rounded: int | None = None) -> Image.Image:
    img = Image.new("RGBA", size, (238, 242, 248, 255))
    draw = ImageDraw.Draw(img)
    draw.text((size[0] // 2 - 18, size[1] // 2 - 12), "B", fill=BILI_PINK)
    if rounded:
        return _round_image(img, rounded)
    return _circle_image(img)


def _circle_image(img: Image.Image) -> Image.Image:
    size = min(img.size)
    img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    img.putalpha(mask)
    return img


def _round_image(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, img.size[0], img.size[1]), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def _format_time(timestamp: int | None) -> str:
    if not timestamp:
        return "刚刚"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _format_count(value: int | None) -> str:
    if value is None or value < 0:
        return "0"
    if value < 10000:
        return str(value)
    if value < 100000000:
        return f"{value / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{value / 100000000:.1f}".rstrip("0").rstrip(".") + "亿"


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _normalize_text(text: str) -> str:
    return "".join((text or "").split()).strip()


def _split_emoji_runs(text: str) -> list[tuple[str, bool]]:
    if not text:
        return []
    runs: list[tuple[str, bool]] = []
    buffer: list[str] = []
    current_is_emoji: bool | None = None
    for char in text:
        is_emoji = _is_emoji_char(char)
        if current_is_emoji is None:
            current_is_emoji = is_emoji
        if is_emoji != current_is_emoji:
            runs.append(("".join(buffer), bool(current_is_emoji)))
            buffer = []
            current_is_emoji = is_emoji
        buffer.append(char)
    if buffer:
        runs.append(("".join(buffer), bool(current_is_emoji)))
    return runs


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or code in {0x200D, 0xFE0F}
    )
