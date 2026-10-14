from __future__ import annotations

import asyncio
import ctypes
import logging
import time
from ctypes import wintypes
from pathlib import Path

from .base import SendResult, Target

log = logging.getLogger(__name__)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_ATTEMPTS = 12
OPEN_RETRY_DELAY = 0.05


def _open_clipboard(user32: ctypes.WinDLL) -> None:
    # Буфер обмена в Windows постоянно занимают другие приложения, один вызов
    # OpenClipboard часто падает с ERROR_ACCESS_DENIED — отсюда повторы.
    for attempt in range(OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            return
        if attempt == OPEN_ATTEMPTS - 1:
            raise OSError(ctypes.get_last_error(), "не удалось открыть буфер обмена")
        time.sleep(OPEN_RETRY_DELAY)


def _copy_to_clipboard(text: str) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    _open_clipboard(user32)

    try:
        user32.EmptyClipboard()
        raw = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
        if not handle:
            raise OSError(ctypes.get_last_error(), "не удалось выделить память под текст")

        locked = kernel32.GlobalLock(handle)
        try:
            ctypes.memmove(locked, raw, len(raw))
        finally:
            kernel32.GlobalUnlock(handle)

        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError(ctypes.get_last_error(), "не удалось записать в буфер обмена")
    finally:
        user32.CloseClipboard()


class ClipboardSender:
    mode = "clipboard"
    supports_photos = False
    ready = True

    async def send(
        self, text: str, targets: list[Target], photos: list[Path]
    ) -> list[SendResult]:
        note = (
            f"фото придётся прикрепить вручную ({len(photos)} шт.)"
            if photos
            else "текст скопирован — вставь в чат"
        )
        try:
            await asyncio.to_thread(_copy_to_clipboard, text)
        except OSError as err:
            log.exception("Не удалось скопировать текст в буфер обмена")
            return [SendResult(target=ch, ok=False, detail=f"буфер обмена: {err}") for ch in targets]

        return [SendResult(target=ch, ok=True, detail=note, attachments=len(photos)) for ch in targets]