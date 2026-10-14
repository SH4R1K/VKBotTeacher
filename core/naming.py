from __future__ import annotations

import re

UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(value: str, *, fallback: str = "file", limit: int = 120) -> str:
    cleaned = re.sub(r"\s+", "_", UNSAFE.sub("", value)).strip().rstrip(".")
    return cleaned[:limit].strip("._") or fallback