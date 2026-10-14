from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from core.config import (
    LLM_PROVIDERS,
    ROOT,
    TRANSPORT_MODES,
    AppConfig,
    LLMConfig,
    load_config,
)
from core.config_store import save_config
from core.history import DEFAULT_PAGE_SIZE, delete_history, history_page, save_message
from core.llm import LLMClient, LLMError, list_model_details, probe
from core.naming import safe_filename
from core.pdf_parser import Lesson, parse_lesson
from core.prompt_builder import build_prompt, load_style
from core.thumbs import ThumbnailError, make_thumb, thumb_path
from core.vk_api import (
    VKError,
    check_token,
    diagnose_chat,
    explain,
    fetch_conversations,
    test_send,
)
from sender import (
    MULTICHAT_MIN_PEER_ID,
    ClipboardSender,
    Sender,
    SendResult,
    Target,
    build_sender,
    suspicious_peer_id,
)

log = logging.getLogger("app")

WEB_DIR = ROOT / "web"
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
# Провайдер ИИ опрашивается не чаще этого, иначе /models долбит каждый клик.
LLM_HEALTH_TTL = 15.0
LLM_HEALTH_PROBE_TIMEOUT = 10.0


@dataclass(slots=True)
class Context:
    config: AppConfig
    client: LLMClient
    sender: Sender
    clipboard: ClipboardSender
    lessons_dir: Path
    photos_dir: Path
    history_dir: Path
    style_path: Path
    llm_health: dict
    llm_health_stamp: float
    llm_health_key: str
    llm_lock: asyncio.Lock

    def lesson(self, name: str) -> Path:
        return _resolve(self.lessons_dir, name)

    def photo(self, name: str) -> Path:
        return _resolve(self.photos_dir, name)

    def history(self, name: str) -> Path:
        return _resolve(self.history_dir, name)

    def apply(self, config: AppConfig) -> None:
        self.config = config
        self.client = LLMClient(config.llm)
        self.sender = build_sender(config)
        self.llm_health = health_unknown()
        self.llm_health_stamp = 0.0
        self.llm_health_key = ""


def _resolve(base: Path, name: str) -> Path:
    # Path(name).name отбрасывает и "..", и абсолютные пути — результат всегда внутри base.
    return base / Path(name).name


def health_unknown() -> dict:
    return {"status": "unknown", "error": "", "checked_at": None}


def _health_key(llm: LLMConfig) -> str:
    return f"{llm.base_url}|{llm.model}|{llm.api_key}"


async def probe_llm(ctx: Context, *, force: bool = False) -> dict:
    """Живость провайдера ИИ с кэшем: без него кнопка генерации запускает пустой таймер."""
    key = _health_key(ctx.config.llm)
    async with ctx.llm_lock:
        fresh = (
            not force
            and key == ctx.llm_health_key
            and ctx.llm_health_stamp > 0
            and time.monotonic() - ctx.llm_health_stamp < LLM_HEALTH_TTL
        )
        if fresh:
            return ctx.llm_health
        ctx.llm_health_key = key
        ctx.llm_health = {"status": "checking", "error": "", "checked_at": None}
        try:
            await probe(ctx.config.llm, timeout_seconds=LLM_HEALTH_PROBE_TIMEOUT)
        except LLMError as err:
            log.info("Провайдер ИИ недоступен: %s", err)
            ctx.llm_health = {"status": "bad", "error": str(err), "checked_at": time.time()}
        else:
            ctx.llm_health = {"status": "ok", "error": "", "checked_at": time.time()}
        ctx.llm_health_stamp = time.monotonic()
        return ctx.llm_health


async def _require_llm(ctx: Context) -> None:
    health = await probe_llm(ctx)
    if health["status"] != "ok":
        reason = str(health["error"] or "проверка не выполнена")
        raise LLMError(f"Провайдер ИИ недоступен, генерация не запущена. {reason}")


def _unique(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _open_file(path: Path) -> None:
    startfile = getattr(os, "startfile", None)  # есть только в Windows
    if startfile is not None:
        startfile(path)
        return
    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def _json(data: object, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def _body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as err:
        raise web.HTTPBadRequest(text="Ожидался JSON") from err
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Ожидался JSON-объект")
    return payload


def _lesson_dict(lesson: Lesson) -> dict:
    return {
        "code": lesson.code,
        "title": lesson.title,
        "summary": lesson.summary,
        "outcomes": lesson.outcomes,
        "stages": [
            {"time_min": s.time_min, "name": s.name, "tasks": s.tasks} for s in lesson.stages
        ],
        "is_usable": lesson.is_usable,
        "has_structure": lesson.has_structure,
        "has_outcomes": lesson.has_outcomes,
    }


async def _generate(ctx: Context, path: Path, temperature: float | None) -> tuple[str, Lesson]:
    lesson = await asyncio.to_thread(parse_lesson, path)
    prompt = await asyncio.to_thread(build_prompt, load_style(ctx.style_path), lesson, path)
    text = await ctx.client.complete(prompt, temperature=temperature)
    return text, lesson


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as err:
        return _json({"error": err.text or err.reason}, status=err.status)
    except LLMError as err:
        log.warning("Ошибка LLM в %s: %s", request.path, err)
        return _json({"error": str(err)}, status=502)
    except Exception as err:
        log.exception("Непредвиденная ошибка %s", request.path)
        return _json({"error": f"{type(err).__name__}: {err}"}, status=500)


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def asset(request: web.Request) -> web.FileResponse:
    path = _resolve(WEB_DIR, request.match_info["name"])
    if not path.is_file():
        raise web.HTTPNotFound(text="Нет такого файла")
    guessed, _ = mimetypes.guess_type(path.name)
    if path.suffix.lower() in {".html", ".js", ".css", ".json", ".svg"}:
        content_type = f"{guessed or 'text/plain'}; charset=utf-8"
    else:
        content_type = guessed or "application/octet-stream"
    return web.FileResponse(
        path, headers={"Content-Type": content_type}
    )


async def state(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    return _json(
        {
            "transport": {
                "mode": ctx.sender.mode,
                "supports_photos": ctx.sender.supports_photos,
                "ready": ctx.sender.ready,
            },
            "llm": {
                "base_url": ctx.config.llm.base_url,
                "model": ctx.config.llm.model,
                "health": ctx.llm_health,
            },
            "chats": [
                {"name": c.name, "peer_id": c.peer_id, "api_capable": c.peer_id is not None}
                for c in ctx.config.chats
            ],
            "lessons": sorted(p.name for p in ctx.lessons_dir.glob("*.pdf")),
            "photos": sorted(p.name for p in ctx.photos_dir.iterdir() if p.is_file()),
            # mtime файла = когда фото попало в папку; по нему сортируется список.
            "photos_meta": [
                {"name": p.name, "added": p.stat().st_mtime}
                for p in sorted(ctx.photos_dir.iterdir(), key=lambda i: (-i.stat().st_mtime, i.name))
                if p.is_file()
            ],
            "style_path": ctx.style_path.name,
        }
    )


async def upload(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    lessons: list[str] = []
    photos: list[str] = []
    reader = await request.multipart()
    while (part := await reader.next()) is not None:
        # Клиент кодирует не-ASCII имя файла percent-escape, сервер отдаёт как есть.
        filename = unquote(part.filename or "")
        if not filename:
            continue
        raw = await part.read(decode=False)
        if not raw:
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_UPLOAD_BYTES, actual_size=len(raw)
            )
        suffix = Path(filename).suffix.lower()
        if suffix == ".pdf":
            target_dir, bucket = ctx.lessons_dir, lessons
        elif suffix in PHOTO_SUFFIXES:
            target_dir, bucket = ctx.photos_dir, photos
        else:
            log.info("Пропущен файл неподходящего типа: %s", filename)
            continue
        stem = safe_filename(Path(filename).stem)
        saved = _unique(target_dir, f"{stem}{suffix}")
        saved.write_bytes(raw)
        bucket.append(saved.name)
    return _json({"lessons": lessons, "photos": photos})


def _photo_path(ctx: Context, name: str) -> Path:
    path = ctx.photo(name)
    if path.suffix.lower() not in PHOTO_SUFFIXES or not path.is_file():
        raise web.HTTPNotFound(text=f"Фото не найдено: {path.name}")
    return path


async def photo_file(request: web.Request) -> web.FileResponse:
    ctx: Context = request.app["ctx"]
    path = _photo_path(ctx, request.query.get("name", ""))
    return web.FileResponse(path)


async def photo_thumb(request: web.Request) -> web.FileResponse:
    ctx: Context = request.app["ctx"]
    path = _photo_path(ctx, request.query.get("name", ""))
    target = thumb_path(ctx.photos_dir, path.name)
    if not target.is_file():
        try:
            await asyncio.to_thread(make_thumb, path, target)
        except ThumbnailError as err:
            # Кривое фото не должно ломать страницу — показываем оригинал.
            log.warning("Миниатюра не получилась, отдаю оригинал: %s", err)
            return web.FileResponse(path)
    return web.FileResponse(target, headers={"Cache-Control": "private, max-age=3600"})


async def photo_delete(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    path = _photo_path(ctx, str((await _body(request)).get("name", "")))
    try:
        path.unlink()
        thumb_path(ctx.photos_dir, path.name).unlink(missing_ok=True)
    except OSError as err:
        raise web.HTTPBadRequest(text=f"Не удалось удалить {path.name}: {err}") from err
    log.info("Фото удалено: %s", path.name)
    return _json({"name": path.name})


async def parse(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    path = ctx.lesson(str((await _body(request)).get("name", "")))
    if not path.exists():
        raise web.HTTPNotFound(text=f"Методичка не найдена: {path.name}")
    lesson = await asyncio.to_thread(parse_lesson, path)
    return _json({"name": path.name, "lesson": _lesson_dict(lesson)})


async def open_lesson(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    name = str((await _body(request)).get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(text="Не указано имя методички.")
    path = ctx.lesson(name)
    if not path.is_file():
        raise web.HTTPNotFound(text=f"Методичка не найдена: {path.name}")
    try:
        await asyncio.to_thread(_open_file, path)
    except OSError as err:
        raise web.HTTPBadRequest(text=f"Не удалось открыть {path.name}: {err}") from err
    log.info("Методичка открыта на компьютере: %s", path.name)
    return _json({"name": path.name})


async def generate(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    path = ctx.lesson(str(payload.get("name", "")))
    if not path.exists():
        raise web.HTTPNotFound(text=f"Методичка не найдена: {path.name}")
    await _require_llm(ctx)
    temperature = payload.get("temperature")
    text, lesson = await _generate(
        ctx, path, float(temperature) if temperature is not None else None
    )
    return _json({"name": path.name, "text": text, "lesson": _lesson_dict(lesson)})


async def generate_batch(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    names = [str(n) for n in (await _body(request)).get("names", [])]
    paths = []
    for name in names:
        path = ctx.lesson(name)
        if path.exists():
            paths.append(path)
    if not paths:
        raise web.HTTPBadRequest(text="Не выбрано ни одной методички")
    await _require_llm(ctx)

    async def run(path: Path) -> dict:
        try:
            text, lesson = await _generate(ctx, path, None)
        except (LLMError, OSError) as err:
            return {"name": path.name, "error": str(err)}
        return {
            "name": path.name,
            "title": f"{lesson.code} {lesson.title}".strip(),
            "text": text,
            "lesson": _lesson_dict(lesson),
        }

    items = await asyncio.gather(*(run(p) for p in paths))
    return _json({"items": list(items)})


async def save(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    title = str(payload.get("title") or "lesson")
    text = str(payload.get("text") or "")
    if not text.strip():
        raise web.HTTPBadRequest(text="Пустое сообщение не сохраняю")
    path = await asyncio.to_thread(save_message, ctx.history_dir, title, text)
    return _json({"name": path.name})


def _query_int(request: web.Request, name: str, default: int) -> int:
    raw = request.query.get(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        log.info("Игнорирую %s=%r в %s: не число", name, raw, request.path)
        return default


async def history(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    page = await asyncio.to_thread(
        history_page,
        ctx.history_dir,
        search=request.query.get("search", ""),
        page=_query_int(request, "page", 1),
        size=_query_int(request, "size", DEFAULT_PAGE_SIZE),
    )
    return _json(
        {
            "items": [
                {"name": e.path.name, "title": e.title, "saved_at": e.saved_at} for e in page.items
            ],
            "total": page.total,
            "page": page.page,
            "pages": page.pages,
            "size": DEFAULT_PAGE_SIZE,
        }
    )


async def history_delete(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    path = ctx.history(str((await _body(request)).get("name", "")))
    if not await asyncio.to_thread(delete_history, path):
        raise web.HTTPNotFound(text=f"Запись не найдена: {path.name}")
    return _json({"name": path.name})


async def history_read(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    path = _resolve(ctx.history_dir, request.query.get("name", ""))
    if not path.exists():
        raise web.HTTPNotFound(text=f"Запись не найдена: {path.name}")
    return _json({"name": path.name, "text": await asyncio.to_thread(path.read_text, "utf-8")})


async def copy(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    text = str((await _body(request)).get("text") or "")
    if not text.strip():
        raise web.HTTPBadRequest(text="Нечего копировать")
    results = await ctx.clipboard.send(text, [Target("Буфер обмена")], [])
    result = results[0]
    return _json({"ok": result.ok, "detail": result.detail})


async def send(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    text = str(payload.get("text") or "")
    if not text.strip():
        raise web.HTTPBadRequest(text="Пустое сообщение не отправляю")

    wanted = {str(name) for name in payload.get("chats", [])}
    targets = [Target(c.name, c.peer_id) for c in ctx.config.chats if c.name in wanted]
    unknown = wanted - {c.name for c in ctx.config.chats}
    if unknown:
        raise web.HTTPBadRequest(text="Нет таких чатов в config.toml: " + ", ".join(sorted(unknown)))

    photos: list[Path] = []
    missing: list[str] = []
    for name in payload.get("photos", []):
        path = ctx.photo(str(name))
        if path.exists():
            photos.append(path)
        else:
            missing.append(str(name))

    results = await ctx.sender.send(text, targets, photos)
    await asyncio.to_thread(save_message, ctx.history_dir, str(payload.get("title") or "lesson"), text)
    return _json({"results": [_result_dict(r) for r in results], "missing_photos": missing})


def _result_dict(result: SendResult) -> dict:
    return {
        "name": result.target.name,
        "ok": result.ok,
        "detail": result.detail,
        "attachments": result.attachments,
    }


async def style_get(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    return _json({"text": await asyncio.to_thread(load_style, ctx.style_path)})


async def style_save(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    text = str((await _body(request)).get("text") or "")
    if not text.strip():
        raise web.HTTPBadRequest(text="Пустой стиль не сохраняю")
    await asyncio.to_thread(ctx.style_path.write_text, text, "utf-8")
    return _json({"ok": True})


def _settings_dict(config: AppConfig) -> dict:
    chats = [{"name": c.name, "peer_id": c.peer_id} for c in config.chats]
    warnings = [
        f"{c.name}: peer_id {c.peer_id} не похож на беседу "
        f"(беседа начинается с {MULTICHAT_MIN_PEER_ID})"
        for c in config.chats
        if suspicious_peer_id(c.peer_id)
    ]
    llm = config.llm
    if LLM_PROVIDERS[llm.provider]["needs_key"] and not llm.api_key.strip():
        warnings.append(
            f"Провайдер ИИ «{LLM_PROVIDERS[llm.provider]['label']}» просит API-ключ, "
            "а он пустой — генерация вернёт ошибку 401."
        )
    return {
        "mode": config.transport.mode,
        "options": list(TRANSPORT_MODES),
        "vk": {
            "user_token": config.vk.user_token,
            "community_token": config.vk.community_token,
            "api_version": config.vk.api_version,
        },
        "llm": {
            "provider": llm.provider,
            "base_url": llm.base_url,
            "api_key": llm.api_key,
            "model": llm.model,
            "temperature": llm.temperature,
            "timeout_seconds": llm.timeout_seconds,
            "providers": [
                {"id": pid, **meta} for pid, meta in LLM_PROVIDERS.items()
            ],
        },
        "chats": chats,
        "warnings": warnings,
    }


def _chats_from_payload(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise web.HTTPBadRequest(text="Список чатов должен быть массивом")
    chats: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise web.HTTPBadRequest(text="Элемент списка чатов должен быть объектом")
        name = str(item.get("name") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="У чата должно быть название")
        raw_peer = item.get("peer_id")
        peer_id: int | None = None
        if raw_peer not in (None, ""):
            try:
                peer_id = int(raw_peer)
            except (TypeError, ValueError) as err:
                raise web.HTTPBadRequest(
                    text=f"peer_id «{raw_peer}» у «{name}» — не число"
                ) from err
        chats.append({"name": name, "peer_id": peer_id})
    # /api/send сопоставляет чаты по имени, поэтому имена обязаны быть уникальны.
    duplicates = {c["name"] for c in chats if [x["name"] for x in chats].count(c["name"]) > 1}
    if duplicates:
        raise web.HTTPBadRequest(
            text="Названия чатов должны быть уникальны: " + ", ".join(sorted(duplicates))
        )
    return chats


def _llm_from_payload(current: LLMConfig, raw: dict) -> dict:
    provider = str(raw.get("provider") or current.provider).strip()
    if provider not in LLM_PROVIDERS:
        raise web.HTTPBadRequest(text=f"Неизвестный провайдер ИИ: {provider}")
    preset = LLM_PROVIDERS[provider]
    data = current.model_dump()
    data["provider"] = provider
    # Пустой адрес у провайдера с готовым — берём готовый, иначе пришлось бы ловить
    # ошибку соединения с пустым URL вместо внятного текста.
    if "base_url" in raw:
        url = str(raw["base_url"] or "").strip()
        if not url and not str(preset["base_url"]).strip():
            raise web.HTTPBadRequest(text="У провайдера «Другой» нужен свой base_url")
        data["base_url"] = url or str(preset["base_url"])
    if "api_key" in raw:
        data["api_key"] = str(raw["api_key"] or "").strip()
    for key, kind in (("model", str), ("temperature", float), ("timeout_seconds", int)):
        if key not in raw:
            continue
        text = str(raw[key] or "").strip()
        if not text:
            raise web.HTTPBadRequest(text=f"Поле ИИ «{key}» не должно быть пустым")
        try:
            data[key] = kind(text)
        except ValueError as err:
            raise web.HTTPBadRequest(text=f"Поле ИИ «{key}»: «{text}» — не число") from err
    return data


def _config_from_payload(current: AppConfig, payload: dict) -> AppConfig:
    mode = str(payload.get("mode") or current.transport.mode).strip()
    if mode not in TRANSPORT_MODES:
        raise web.HTTPBadRequest(text=f"Неизвестный режим отправки: {mode}")
    vk_in = payload.get("vk") if isinstance(payload.get("vk"), dict) else {}
    llm_in = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
    data = current.model_dump()
    data["transport"] = {"mode": mode}
    data["vk"] = {
        "user_token": str(vk_in.get("user_token", current.vk.user_token)).strip(),
        "community_token": str(vk_in.get("community_token", current.vk.community_token)).strip(),
        "api_version": str(vk_in.get("api_version") or current.vk.api_version).strip(),
    }
    data["llm"] = _llm_from_payload(current.llm, llm_in)
    data["chats"] = _chats_from_payload(payload.get("chats", current.chats))
    try:
        return AppConfig.model_validate(data)
    except ValidationError as err:
        raise web.HTTPBadRequest(text="Настройки не прошли проверку: " + str(err)) from err


def _resolve_token(config: AppConfig, mode: str, override: object) -> str:
    token = str(override or "").strip()
    if token:
        return token
    return config.vk.community_token if mode == "community" else config.vk.user_token


async def settings_get(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    return _json(_settings_dict(ctx.config))


async def settings_save(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    config = _config_from_payload(ctx.config, await _body(request))
    await asyncio.to_thread(save_config, config)
    ctx.apply(config)
    log.info(
        "Настройки сохранены: транспорт=%s, чатов=%d", config.transport.mode, len(config.chats)
    )
    return _json(_settings_dict(config))


async def llm_check(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    raw = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
    # Проверяем ровно то, что сейчас в полях диалога, а не сохранённый config.
    draft = _llm_from_payload(ctx.config.llm, raw)
    try:
        llm = LLMConfig.model_validate(draft)
    except ValidationError as err:
        raise web.HTTPBadRequest(text="Настройки ИИ не прошли проверку: " + str(err)) from err
    try:
        details = await list_model_details(llm)
    except LLMError as err:
        log.info("Проверка ИИ не удалась: %s", err)
        return _json({"ok": False, "error": str(err)})
    models = [item["id"] for item in details]
    return _json({"ok": True, "models": models, "details": details, "model_found": llm.model in models})


async def llm_health(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    force = request.query.get("force") == "1"
    return _json({"llm": await probe_llm(ctx, force=force)})


async def vk_check(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    mode = str(payload.get("mode") or ctx.config.transport.mode)
    token = _resolve_token(ctx.config, mode, payload.get("token"))
    try:
        info = await check_token(token, ctx.config.vk.api_version, mode)
    except (VKError, aiohttp.ClientError) as err:
        log.info("Проверка токена не удалась: %s", err)
        return _json({"ok": False, "error": str(err)})
    return _json({"ok": True, **info})


async def vk_conversations(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    mode = str(payload.get("mode") or "user_token")
    token = _resolve_token(ctx.config, mode, payload.get("token"))
    try:
        items = await fetch_conversations(token, ctx.config.vk.api_version)
    except (VKError, aiohttp.ClientError) as err:
        log.info("Загрузка бесед не удалась: %s", err)
        return _json({"ok": False, "error": str(err)})
    known = {c.peer_id for c in ctx.config.chats if c.peer_id is not None}
    for item in items:
        item["in_list"] = item["peer_id"] in known
    return _json({"ok": True, "items": items})


async def vk_chat_check(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    mode = str(payload.get("mode") or ctx.config.transport.mode)
    token = _resolve_token(ctx.config, mode, payload.get("token"))
    raw_peer = payload.get("peer_id")
    try:
        peer_id = int(str(raw_peer).strip())
    except (TypeError, ValueError) as err:
        raise web.HTTPBadRequest(text="peer_id должен быть числом") from err
    try:
        result = await diagnose_chat(token, peer_id, ctx.config.vk.api_version, mode)
    except (VKError, aiohttp.ClientError) as err:
        log.info("Диагностика беседы не удалась: %s", err)
        return _json({"ok": False, "error": str(err)})
    return _json(result)


async def vk_chat_test_send(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    payload = await _body(request)
    mode = str(payload.get("mode") or ctx.config.transport.mode)
    token = _resolve_token(ctx.config, mode, payload.get("token"))
    raw_peer = payload.get("peer_id")
    try:
        peer_id = int(str(raw_peer).strip())
    except (TypeError, ValueError) as err:
        raise web.HTTPBadRequest(text="peer_id должен быть числом") from err
    try:
        result = await test_send(token, peer_id, ctx.config.vk.api_version)
    except (VKError, aiohttp.ClientError) as err:
        log.info("Тестовая отправка не прошла: %s", err)
        return _json({"ok": False, "error": explain(err)})
    return _json(result)


def create_app() -> web.Application:
    config = load_config()
    ctx = Context(
        config=config,
        client=LLMClient(config.llm),
        sender=build_sender(config),
        clipboard=ClipboardSender(),
        lessons_dir=(ROOT / config.paths.lessons_dir).resolve(),
        photos_dir=(ROOT / config.paths.photos_dir).resolve(),
        history_dir=(ROOT / config.paths.history_dir).resolve(),
        style_path=(ROOT / config.paths.style_file).resolve(),
        llm_health=health_unknown(),
        llm_health_stamp=0.0,
        llm_health_key="",
        llm_lock=asyncio.Lock(),
    )
    for directory in (ctx.lessons_dir, ctx.photos_dir, ctx.history_dir):
        directory.mkdir(parents=True, exist_ok=True)

    app = web.Application(middlewares=[errors])
    app["ctx"] = ctx
    app.router.add_routes(
        [
            web.get("/", index),
            web.get("/assets/{name}", asset),
            web.get("/api/state", state),
            web.post("/api/upload", upload),
            web.get("/api/photo", photo_file),
            web.get("/api/photo/thumb", photo_thumb),
            web.post("/api/photo/delete", photo_delete),
            web.post("/api/parse", parse),
            web.post("/api/lesson/open", open_lesson),
            web.post("/api/generate", generate),
            web.post("/api/generate_batch", generate_batch),
            web.post("/api/save", save),
            web.get("/api/history", history),
            web.get("/api/history/read", history_read),
            web.post("/api/history/delete", history_delete),
            web.post("/api/copy", copy),
            web.post("/api/send", send),
            web.get("/api/style", style_get),
            web.post("/api/style", style_save),
            web.get("/api/settings", settings_get),
            web.post("/api/settings", settings_save),
            web.post("/api/llm/check", llm_check),
    web.get("/api/llm/health", llm_health),
            web.post("/api/vk/check", vk_check),
            web.post("/api/vk/conversations", vk_conversations),
            web.post("/api/vk/chat", vk_chat_check),
            web.post("/api/vk/chat/send-test", vk_chat_test_send),
        ]
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = load_config()
    app = create_app()
    url = f"http://{config.server.host}:{config.server.port}/"

    async def open_browser(_: web.Application) -> None:
        webbrowser.open(url)

    log.info("Приложение: %s", url)
    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        print=None,
        on_startup=[open_browser],
    )


if __name__ == "__main__":
    main()