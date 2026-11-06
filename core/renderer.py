from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path

import typst

from astrbot.api import logger

from .models import DynamicItem, MonitorTemplate

CANVAS_WIDTH = 960
PPI = 72.0
PAGE_MARGIN = 24
CARD_PADDING = 30
CONTENT_WIDTH = CANVAS_WIDTH - (PAGE_MARGIN + CARD_PADDING) * 2

BODY_SIZE = 29
BODY_MAX_LINES = 8
BODY_MAX_LINES_WITH_VIDEO = 5
VIDEO_TITLE_SIZE = 32
VIDEO_TITLE_MAX_LINES = 2
VIDEO_CARD_PADDING = 22
VIDEO_COVER_WIDTH = 336
VIDEO_GAP = 24
VIDEO_TEXT_WIDTH = (
    CONTENT_WIDTH - VIDEO_CARD_PADDING * 2 - VIDEO_COVER_WIDTH - VIDEO_GAP
)
VIDEO_DESC_WIDTH = CONTENT_WIDTH - VIDEO_CARD_PADDING * 2
SECONDARY_SIZE = 22
VIDEO_DESC_MAX_LINES = 4
FORWARD_CARD_PADDING = 24
FORWARD_WIDTH = CONTENT_WIDTH - FORWARD_CARD_PADDING * 2
FORWARD_SUMMARY_MAX_LINES = 2

MEDIA_CACHE_DIR_NAME = "media"
FONT_CACHE_DIR_NAME = "fonts"
EMOJI_CACHE_DIR_NAME = "emoji"
FONT_DOWNLOAD_TIMEOUT = 30
FONT_DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024
EMOJI_DOWNLOAD_TIMEOUT = 10
EMOJI_DOWNLOAD_MAX_BYTES = 512 * 1024
NEGATIVE_CACHE_TTL = 3600
CLAMP_SAFETY_FACTOR = 0.95
CHINESE_FONT_FILENAME = "wqy-microhei.ttc"
CHINESE_FONT_URLS = [
    "https://raw.githubusercontent.com/anthonyfok/fonts-wqy-microhei/master/wqy-microhei.ttc",
    "https://github.com/anthonyfok/fonts-wqy-microhei/raw/master/wqy-microhei.ttc",
]
TWEMOJI_BASE_URLS = [
    "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72",
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72",
]
FONT_MAGIC = (b"ttcf", b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1")
IMAGE_EXTENSIONS = (".png", ".jpg", ".gif", ".webp", ".svg")


class DynamicRenderer:
    """Render Bilibili dynamic cards with Typst.

    Attributes:
        temp_dir: Directory that receives the rendered PNG files.
        cache_dir: Plugin cache directory; it is also the Typst root so every
            image referenced by the template can use a relative path.
        timeout: Network timeout in seconds for media downloads.
    """

    def __init__(self, temp_dir: Path, cache_dir: Path, *, timeout: int = 20) -> None:
        """Initialize the renderer and prepare cache directories.

        Args:
            temp_dir: Directory that receives rendered PNG files.
            cache_dir: Plugin cache directory, used as the Typst root.
            timeout: Network timeout in seconds for media downloads.
        """
        self.temp_dir = temp_dir
        self.cache_dir = cache_dir
        self.media_dir = self.cache_dir / MEDIA_CACHE_DIR_NAME
        self.font_dir = self.cache_dir / FONT_CACHE_DIR_NAME
        self.emoji_dir = self.cache_dir / EMOJI_CACHE_DIR_NAME
        self.timeout = timeout
        self.root = self.cache_dir.parent
        self.template = (Path(__file__).with_name("card.typ")).read_bytes()

        for directory in (
            self.temp_dir,
            self.cache_dir,
            self.media_dir,
            self.font_dir,
            self.emoji_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.font_path = _resolve_chinese_font(self.font_dir)
        self._compiler: typst.Compiler | None = None
        self._lock = threading.Lock()
        self._failed_media: dict[str, float] = {}
        self._failed_emoji: dict[str, float] = {}

    async def render(self, dynamic: DynamicItem, template: MonitorTemplate) -> Path:
        """Render one dynamic into a PNG file.

        Args:
            dynamic: The dynamic to render.
            template: Monitor template that triggered the push. It is accepted for
                call-site compatibility and does not affect the layout.

        Returns:
            Path to the rendered PNG file.

        Raises:
            typst.TypstError: If the Typst template fails to compile.
        """
        del template
        return await asyncio.to_thread(self._render_sync, dynamic)

    async def cleanup_temp(self, *, older_than_seconds: int = 86400) -> None:
        """Remove rendered images and cached media older than the given age.

        Args:
            older_than_seconds: Age threshold in seconds.
        """
        cutoff = time.time() - older_than_seconds
        patterns = (
            (self.temp_dir, "bilibili_*.png"),
            (self.media_dir, "*"),
        )
        for directory, pattern in patterns:
            for path in directory.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("[BilibiliPosts] 清理临时文件失败：%s", path)

    def _render_sync(self, dynamic: DynamicItem) -> Path:
        """Build the payload and compile the Typst template into a PNG file.

        Args:
            dynamic: The dynamic to render.

        Returns:
            Path to the rendered PNG file.
        """
        payload = self._build_payload(dynamic)
        stamp = int(time.time())
        output = self.temp_dir / f"bilibili_{_safe_filename(dynamic.id)}_{stamp}.png"
        with self._lock:
            compiler = self._get_compiler()
            compiler.compile(
                input=self.template,
                output=output.as_posix(),
                format="png",
                ppi=PPI,
                sys_inputs={"data": json.dumps(payload, ensure_ascii=False)},
            )
        return output

    def _get_compiler(self) -> typst.Compiler:
        """Return the lazily created Typst compiler.

        Returns:
            A compiler bound to the plugin cache root with CJK fonts available.
        """
        if self._compiler is None:
            font_paths = [self.font_dir.as_posix()] if self.font_path else []
            self._compiler = typst.Compiler(root=self.root, font_paths=font_paths)
        return self._compiler

    def _build_payload(self, dynamic: DynamicItem) -> dict:
        """Build the Typst template payload for a dynamic.

        Args:
            dynamic: The dynamic to render.

        Returns:
            Template data consumed by ``card.typ``.
        """
        max_lines = BODY_MAX_LINES_WITH_VIDEO if dynamic.video else BODY_MAX_LINES
        stats = dynamic.stats
        has_stats = any(
            value is not None for value in (stats.like, stats.comment, stats.forward)
        )

        payload: dict = {
            "avatar": self._media_path(dynamic.author.face),
            "author": dynamic.author.name or "Bilibili 用户",
            "uid": f"UID {dynamic.author.uid}"
            if dynamic.author.uid
            else "Bilibili Dynamic",
            "published": _format_time(dynamic.publish_time),
            "text": self._rich(
                _clamp_text(
                    _post_text(dynamic),
                    width=CONTENT_WIDTH,
                    size=BODY_SIZE,
                    max_lines=max_lines,
                )
            ),
            "video": None,
            "images": [],
            "orig": None,
            "stats": [],
            "footer_left": "Bilibili 动态检测",
            "footer_right": _truncate_link(dynamic.url or "https://www.bilibili.com"),
        }

        if dynamic.video and dynamic.video.cover:
            payload["video"] = {
                "cover": self._media_path(dynamic.video.cover),
                "title": _clamp_text(
                    dynamic.video.title or dynamic.title,
                    width=VIDEO_TEXT_WIDTH,
                    size=VIDEO_TITLE_SIZE,
                    max_lines=VIDEO_TITLE_MAX_LINES,
                ),
                "duration": dynamic.video.duration,
                "desc": _clamp_text(
                    dynamic.video.desc,
                    width=VIDEO_DESC_WIDTH,
                    size=SECONDARY_SIZE,
                    max_lines=VIDEO_DESC_MAX_LINES,
                ),
            }
        elif dynamic.images:
            payload["images"] = [
                self._media_path(url) for url in list(dynamic.images)[:9]
            ]

        if dynamic.orig:
            payload["orig"] = {
                "author": dynamic.orig.author.name or "原作者",
                "summary": _clamp_text(
                    dynamic.orig.title or dynamic.orig.text or "原动态内容",
                    width=FORWARD_WIDTH,
                    size=SECONDARY_SIZE,
                    max_lines=FORWARD_SUMMARY_MAX_LINES,
                ),
            }

        if has_stats:
            payload["stats"] = [
                {"icon": self._rich("👍"), "value": _format_count(stats.like)},
                {"icon": self._rich("💬"), "value": _format_count(stats.comment)},
                {"icon": self._rich("↗"), "value": _format_count(stats.forward)},
            ]

        return payload

    def _rich(self, text: str) -> list[dict]:
        """Split text into plain text and twemoji image tokens.

        Args:
            text: Raw text that may contain emoji.

        Returns:
            Token list consumed by the ``rich`` helper in ``card.typ``.
        """
        tokens: list[dict] = []
        for segment, is_emoji in _split_text_tokens(text):
            if is_emoji:
                path = self._emoji_path(segment)
                if path:
                    tokens.append({"emoji": path})
                    continue
            tokens.append({"text": segment})
        return tokens

    def _media_path(self, url: str) -> str:
        """Download and cache an image, then return its Typst-relative path.

        Args:
            url: Remote image URL.

        Returns:
            Root-relative POSIX path, or an empty string when unavailable.
        """
        if not url:
            return ""
        if _recently_failed(self._failed_media, url):
            return ""
        key = hashlib.sha256(url.encode()).hexdigest()
        for extension in IMAGE_EXTENSIONS:
            cached = self.media_dir / f"{key}{extension}"
            if cached.exists():
                return self._relative(cached)

        try:
            data = _download_sync(url, self.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BilibiliPosts] 图片下载失败 %s: %s", url, exc)
            self._failed_media[url] = time.monotonic()
            return ""

        extension = _detect_image_extension(data)
        if not extension:
            logger.debug("[BilibiliPosts] 不支持的图片格式：%s", url)
            self._failed_media[url] = time.monotonic()
            return ""

        path = self.media_dir / f"{key}{extension}"
        path.write_bytes(data)
        return self._relative(path)

    def _emoji_path(self, emoji: str) -> str:
        """Download and cache the twemoji PNG for an emoji.

        Args:
            emoji: Emoji character sequence.

        Returns:
            Root-relative POSIX path, or an empty string when unavailable.
        """
        codepoint = _twemoji_codepoint(emoji)
        if not codepoint:
            return ""
        if _recently_failed(self._failed_emoji, codepoint):
            return ""
        path = self.emoji_dir / f"{codepoint}.png"
        if not path.exists() and not self._download_twemoji(codepoint, path):
            self._failed_emoji[codepoint] = time.monotonic()
            return ""
        return self._relative(path)

    def _download_twemoji(self, codepoint: str, cache_path: Path) -> bool:
        """Download a twemoji asset into the cache.

        Args:
            codepoint: Twemoji codepoint file name.
            cache_path: Destination path.

        Returns:
            True when the asset is available in the cache.
        """
        temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        for base_url in TWEMOJI_BASE_URLS:
            url = f"{base_url}/{codepoint}.png"
            try:
                data = _download_sync(
                    url,
                    EMOJI_DOWNLOAD_TIMEOUT,
                    max_bytes=EMOJI_DOWNLOAD_MAX_BYTES,
                )
                if not _is_png(data):
                    raise ValueError("下载内容不是 PNG")
                temp_path.write_bytes(data)
                temp_path.replace(cache_path)
                return True
            except Exception as exc:  # noqa: BLE001
                temp_path.unlink(missing_ok=True)
                logger.debug("[BilibiliPosts] 下载 Twemoji 失败 %s: %s", url, exc)
        return False

    def _relative(self, path: Path) -> str:
        """Return a Typst-safe path relative to the plugin cache root.

        Args:
            path: Absolute path inside the Typst root.

        Returns:
            POSIX-style relative path.
        """
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()


def _post_text(dynamic: DynamicItem) -> str:
    """Return the body text to render for a dynamic.

    Args:
        dynamic: The dynamic to inspect.

    Returns:
        Post text, or an empty string when the title already covers it.
    """
    text = (dynamic.text or "").strip()
    if dynamic.video:
        title = (dynamic.video.title or "").strip()
        if text and _normalize_text(text) != _normalize_text(title):
            return text
        return ""
    return text or dynamic.title


def _clamp_text(text: str, *, width: float, size: float, max_lines: int) -> str:
    """Approximate line wrapping and truncate oversized text with an ellipsis.

    Typst performs the real line breaking; this estimate only decides whether the
    content must be cut so a single dynamic cannot produce an extremely tall card.

    Args:
        text: Raw text.
        width: Available width in points.
        size: Font size in points.
        max_lines: Maximum number of lines to keep.

    Returns:
        Whitespace-normalized text, optionally truncated with an ellipsis.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return ""

    usable_width = width * CLAMP_SAFETY_FACTOR
    current = 0.0
    lines = 1
    char_pos = 0
    for unit, is_emoji in _iter_text_units(normalized):
        advance = size if is_emoji else _estimate_advance(unit, size)
        if current + advance > usable_width:
            if lines == max_lines:
                return normalized[:char_pos].rstrip(".,，。;；、 ") + "…"
            lines += 1
            current = 0.0
        current += advance
        char_pos += len(unit)
    return normalized


def _iter_text_units(text: str) -> list[tuple[str, bool]]:
    """Flatten text into characters and indivisible emoji sequences.

    Args:
        text: Source text.

    Returns:
        List of units paired with an ``is_emoji`` flag.
    """
    units: list[tuple[str, bool]] = []
    for segment, is_emoji in _split_text_tokens(text):
        if is_emoji:
            units.append((segment, True))
        else:
            units.extend((char, False) for char in segment)
    return units


def _estimate_advance(char: str, size: float) -> float:
    """Estimate the horizontal advance of a character in points.

    Args:
        char: Single character.
        size: Font size in points.

    Returns:
        Estimated advance width in points.
    """
    if char.isspace():
        return size * 0.3
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return size
    if char in "ilj|!.,:;'`":
        return size * 0.3
    if char.isupper() or char.isdigit():
        return size * 0.62
    return size * 0.52


def _recently_failed(cache: dict[str, float], key: str) -> bool:
    """Check whether a resource failed recently and should be skipped.

    Args:
        cache: Failure timestamps keyed by resource identifier.
        key: Resource identifier.

    Returns:
        True while the negative cache entry is still fresh.
    """
    stamp = cache.get(key)
    return stamp is not None and time.monotonic() - stamp < NEGATIVE_CACHE_TTL


def _download_sync(url: str, timeout: int, *, max_bytes: int | None = None) -> bytes:
    """Download a URL synchronously.

    Args:
        url: Target URL.
        timeout: Timeout in seconds.
        max_bytes: Optional size limit.

    Returns:
        Downloaded bytes.

    Raises:
        ValueError: If the payload exceeds ``max_bytes``.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        data = response.read(max_bytes + 1 if max_bytes else -1)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"下载内容超过限制：{max_bytes} bytes")
    return data


def _detect_image_extension(data: bytes) -> str | None:
    """Detect a Typst-supported image extension from the payload header.

    Args:
        data: Image bytes.

    Returns:
        File extension including the dot, or None when unsupported.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if b"<svg" in data[:512]:
        return ".svg"
    return None


def _is_png(data: bytes) -> bool:
    """Check whether the payload is a PNG image.

    Args:
        data: Downloaded bytes.

    Returns:
        True for PNG data.
    """
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _resolve_chinese_font(font_dir: Path) -> Path | None:
    """Resolve or download the CJK font used by the Typst template.

    Args:
        font_dir: Directory that caches downloaded fonts.

    Returns:
        Path to the font file, or None when unavailable.
    """
    downloaded_font = font_dir / CHINESE_FONT_FILENAME
    if _is_valid_font(downloaded_font):
        return downloaded_font

    for url in CHINESE_FONT_URLS:
        try:
            logger.info("[BilibiliPosts] 尝试下载中文字体：%s", url)
            data = _download_sync(
                url,
                FONT_DOWNLOAD_TIMEOUT,
                max_bytes=FONT_DOWNLOAD_MAX_BYTES,
            )
            temp_path = downloaded_font.with_suffix(downloaded_font.suffix + ".tmp")
            temp_path.write_bytes(data)
            if not _is_valid_font(temp_path):
                temp_path.unlink(missing_ok=True)
                raise ValueError("下载文件不是有效字体")
            temp_path.replace(downloaded_font)
            logger.info("[BilibiliPosts] 中文字体已缓存：%s", downloaded_font)
            return downloaded_font
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BilibiliPosts] 下载中文字体失败：%s", exc)
            continue

    logger.warning("[BilibiliPosts] 中文字体不可用，将回退到系统字体。")
    return None


def _is_valid_font(path: Path) -> bool:
    """Check whether a file starts with a known font signature.

    Args:
        path: Candidate font file.

    Returns:
        True for TTC/TTF/OTF payloads.
    """
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("rb") as font_file:
            return font_file.read(4) in FONT_MAGIC
    except OSError:
        return False


def _format_time(timestamp: int | None) -> str:
    """Format a publish timestamp for display.

    Args:
        timestamp: UNIX timestamp.

    Returns:
        Formatted time or "刚刚" when unknown.
    """
    if not timestamp:
        return "刚刚"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _format_count(value: int | None) -> str:
    """Format interaction counters with Chinese units.

    Args:
        value: Raw counter value.

    Returns:
        Human-readable counter.
    """
    if value is None or value < 0:
        return "0"
    if value < 10000:
        return str(value)
    if value < 100000000:
        return f"{value / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{value / 100000000:.1f}".rstrip("0").rstrip(".") + "亿"


def _truncate_link(link: str, limit: int = 52) -> str:
    """Shorten a link for the card footer.

    Args:
        link: Full URL.
        limit: Maximum number of characters.

    Returns:
        Possibly truncated URL.
    """
    return link if len(link) <= limit else link[:limit].rstrip() + "…"


def _safe_filename(value: str) -> str:
    """Sanitize a dynamic id for use in a file name.

    Args:
        value: Raw identifier.

    Returns:
        Identifier containing only safe characters.
    """
    return "".join(char for char in value if char.isalnum() or char in "-_") or "card"


def _normalize_text(text: str) -> str:
    """Collapse all whitespace in a string.

    Args:
        text: Source text.

    Returns:
        Whitespace-free text.
    """
    return "".join((text or "").split()).strip()


def _split_text_tokens(text: str) -> list[tuple[str, bool]]:
    """Split text into plain segments and emoji sequences.

    Args:
        text: Source text.

    Returns:
        Pairs of segment text and an ``is_emoji`` flag.
    """
    if not text:
        return []
    tokens: list[tuple[str, bool]] = []
    buffer: list[str] = []
    chars = list(text)
    index = 0
    while index < len(chars):
        emoji, next_index = _consume_emoji(chars, index)
        if emoji:
            if buffer:
                tokens.append(("".join(buffer), False))
                buffer = []
            tokens.append((emoji, True))
            index = next_index
            continue
        buffer.append(chars[index])
        index += 1
    if buffer:
        tokens.append(("".join(buffer), False))
    return tokens


def _consume_emoji(chars: list[str], index: int) -> tuple[str, int]:
    """Consume one emoji sequence starting at an index.

    Args:
        chars: Character list.
        index: Start index.

    Returns:
        The emoji sequence (empty when none) and the next index.
    """
    char = chars[index]
    if index + 1 < len(chars) and _is_regional_indicator(char):
        next_char = chars[index + 1]
        if _is_regional_indicator(next_char):
            return char + next_char, index + 2

    if _is_keycap_base(char):
        next_index = index + 1
        if next_index < len(chars) and ord(chars[next_index]) == 0xFE0F:
            next_index += 1
        if next_index < len(chars) and ord(chars[next_index]) == 0x20E3:
            return "".join(chars[index : next_index + 1]), next_index + 1
        return "", index

    if not _is_emoji_base(char):
        return "", index

    end = _consume_single_emoji(chars, index)
    while end < len(chars) and ord(chars[end]) == 0x200D:
        next_end = _consume_single_emoji(chars, end + 1)
        if next_end == end + 1:
            break
        end = next_end
    return "".join(chars[index:end]), end


def _consume_single_emoji(chars: list[str], index: int) -> int:
    """Consume one emoji base with optional variation selector and skin tone.

    Args:
        chars: Character list.
        index: Start index.

    Returns:
        Index right after the consumed emoji element.
    """
    if index >= len(chars) or not _is_emoji_base(chars[index]):
        return index
    index += 1
    if index < len(chars) and ord(chars[index]) == 0xFE0F:
        index += 1
    if index < len(chars) and _is_skin_tone(chars[index]):
        index += 1
    return index


def _twemoji_codepoint(emoji: str) -> str:
    """Convert an emoji sequence to its twemoji file name.

    Args:
        emoji: Emoji character sequence.

    Returns:
        Hyphen-separated lowercase codepoints.
    """
    codepoints = [ord(char) for char in emoji]
    normalized: list[int] = []
    for index, codepoint in enumerate(codepoints):
        if codepoint == 0xFE0F and _should_drop_variation_selector(codepoints, index):
            continue
        normalized.append(codepoint)
    return "-".join(f"{codepoint:x}" for codepoint in normalized)


def _should_drop_variation_selector(codepoints: list[int], index: int) -> bool:
    """Decide whether a variation selector is omitted from the twemoji name.

    Args:
        codepoints: Codepoint list.
        index: Index of the variation selector.

    Returns:
        True when the selector should be dropped.
    """
    prev_codepoint = codepoints[index - 1] if index > 0 else None
    next_codepoint = codepoints[index + 1] if index + 1 < len(codepoints) else None
    return (
        prev_codepoint in {0x0023, 0x002A}
        or _is_digit_codepoint(prev_codepoint)
        or next_codepoint != 0x200D
    )


def _is_emoji_base(char: str) -> bool:
    """Check whether a character can start an emoji sequence.

    Args:
        char: Single character.

    Returns:
        True for emoji base characters.
    """
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or _is_regional_indicator(char)
    )


def _is_regional_indicator(char: str) -> bool:
    """Check whether a character is a regional indicator.

    Args:
        char: Single character.

    Returns:
        True for regional indicators.
    """
    code = ord(char)
    return 0x1F1E6 <= code <= 0x1F1FF


def _is_keycap_base(char: str) -> bool:
    """Check whether a character can start a keycap emoji.

    Args:
        char: Single character.

    Returns:
        True for ``#``, ``*`` and digits.
    """
    code = ord(char)
    return code in {0x0023, 0x002A} or _is_digit_codepoint(code)


def _is_digit_codepoint(codepoint: int | None) -> bool:
    """Check whether a codepoint is an ASCII digit.

    Args:
        codepoint: Codepoint or None.

    Returns:
        True for ASCII digits.
    """
    return codepoint is not None and 0x0030 <= codepoint <= 0x0039


def _is_skin_tone(char: str) -> bool:
    """Check whether a character is an emoji skin tone modifier.

    Args:
        char: Single character.

    Returns:
        True for skin tone modifiers.
    """
    code = ord(char)
    return 0x1F3FB <= code <= 0x1F3FF
