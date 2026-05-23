from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

MAX_SEEN_IDS_PER_KEY = 500


@dataclass
class StateEntry:
    seen_ids: list[str] = field(default_factory=list)
    initialized: bool = False


class DynamicStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, StateEntry] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BilibiliPosts] 读取状态文件失败，将使用空状态：%s", exc)
            self._entries = {}
            return

        entries = raw.get("entries") if isinstance(raw, dict) else {}
        if not isinstance(entries, dict):
            self._entries = {}
            return

        self._entries = {}
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            seen_ids = [str(item) for item in value.get("seen_ids", []) if item]
            self._entries[str(key)] = StateEntry(
                seen_ids=seen_ids[-MAX_SEEN_IDS_PER_KEY:],
                initialized=bool(value.get("initialized", False)),
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "version": 1,
            "entries": {
                key: {
                    "seen_ids": entry.seen_ids[-MAX_SEEN_IDS_PER_KEY:],
                    "initialized": entry.initialized,
                }
                for key, entry in self._entries.items()
            },
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)

    def is_initialized(self, key: str) -> bool:
        return self._get_entry(key).initialized

    def mark_initialized(self, key: str) -> None:
        self._get_entry(key).initialized = True

    def has_seen(self, key: str, dynamic_id: str) -> bool:
        return dynamic_id in self._get_entry(key).seen_ids

    def mark_seen(self, key: str, dynamic_id: str) -> None:
        entry = self._get_entry(key)
        if dynamic_id in entry.seen_ids:
            entry.seen_ids.remove(dynamic_id)
        entry.seen_ids.append(dynamic_id)
        entry.seen_ids = entry.seen_ids[-MAX_SEEN_IDS_PER_KEY:]
        entry.initialized = True

    def mark_many_seen(self, key: str, dynamic_ids: list[str]) -> None:
        for dynamic_id in dynamic_ids:
            self.mark_seen(key, dynamic_id)
        self.mark_initialized(key)

    def count_entries(self) -> int:
        return len(self._entries)

    def _get_entry(self, key: str) -> StateEntry:
        if key not in self._entries:
            self._entries[key] = StateEntry()
        return self._entries[key]
