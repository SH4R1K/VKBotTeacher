from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .naming import safe_filename

log = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    path: Path
    title: str
    saved_at: str


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: list[HistoryEntry]
    total: int
    page: int
    pages: int


def _slug(value: str) -> str:
    return safe_filename(value, fallback="lesson", limit=80)


def _readable(value: str) -> str:
    # safe_filename склеивает пробелы в подчёркивания, а вернуть их назад нельзя —
    # поэтому и заголовок, и поиск сравнивают текст с нормализованными пробелами.
    return re.sub(r"[\s_]+", " ", value).strip()


def save_message(history_dir: Path, title: str, text: str) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = history_dir / f"{_slug(title)}_{stamp}.txt"
    suffix = 1
    while path.exists():
        path = history_dir / f"{_slug(title)}_{stamp}_{suffix}.txt"
        suffix += 1
    path.write_text(text, encoding="utf-8")
    log.info("Сообщение сохранено: %s", path.name)
    return path


def list_history(history_dir: Path) -> list[HistoryEntry]:
    if not history_dir.exists():
        return []
    entries: list[HistoryEntry] = []
    for path in history_dir.glob("*.txt"):
        stat = path.stat()
        entries.append(
            HistoryEntry(
                path=path,
                title=_readable(path.stem.rsplit("_", 2)[0] or path.stem),
                saved_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            )
        )
    return sorted(entries, key=lambda e: e.path.stat().st_mtime, reverse=True)


def _matches(entry: HistoryEntry, needle: str) -> bool:
    haystacks = (entry.title, entry.path.stem)
    if any(needle in text for text in haystacks):
        return True
    try:
        body = entry.path.read_text("utf-8", errors="replace")
    except OSError as err:
        log.warning("Не прочитал %s для поиска: %s", entry.path.name, err)
        return False
    return needle in _readable(body).casefold()


def history_page(
    history_dir: Path,
    *,
    search: str = "",
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
) -> HistoryPage:
    entries = list_history(history_dir)
    needle = _readable(search).casefold()
    if needle:
        entries = [entry for entry in entries if _matches(entry, needle)]
    size = max(1, min(int(size), MAX_PAGE_SIZE))
    pages = max(1, -(-len(entries) // size))
    page = min(max(1, int(page)), pages)
    start = (page - 1) * size
    return HistoryPage(
        items=entries[start : start + size], total=len(entries), page=page, pages=pages
    )


def delete_history(path: Path) -> bool:
    if not path.is_file():
        return False
    path.unlink()
    log.info("Запись истории удалена: %s", path.name)
    return True