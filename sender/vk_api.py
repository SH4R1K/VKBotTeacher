from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any

import aiohttp

from core.vk_api import (
    MULTICHAT_MIN_PEER_ID,
    TLS_HINT,
    VK_HINTS,
    VKError,
    explain,
    suspicious_peer_id,
    vk_call,
    vk_session,
)

from .base import SendResult, Target

log = logging.getLogger(__name__)

SEND_DELAY_SECONDS = 0.4
MAX_ATTACHMENTS = 10

__all__ = [
    "MULTICHAT_MIN_PEER_ID",
    "TLS_HINT",
    "VK_HINTS",
    "VKError",
    "VKSender",
    "explain",
    "suspicious_peer_id",
]


class VKSender:
    supports_photos = True

    def __init__(self, token: str, api_version: str = "5.199", mode: str = "user_token") -> None:
        self.mode = mode
        self.token = token
        self.api_version = api_version
        self.ready = bool(token)

    async def _call(self, session: aiohttp.ClientSession, method: str, **params: Any) -> dict:
        return await vk_call(session, method, self.token, self.api_version, **params)

    @staticmethod
    def _photo_ref(photo: object) -> str:
        """Приводит ответ ВК к виду photo{owner_id}_{photo_id}.

        Форматов три: объект, список объектов и строка, в которой лежит JSON
        с объектом — последний приходит от загрузчика файлов, и без разбора
        JSON в attachment попадает мусор, который ВК молча отбрасывает.
        """
        if isinstance(photo, str):
            text = photo.strip()
            if text.startswith(("[", "{")):
                try:
                    photo = json.loads(text)
                except json.JSONDecodeError:
                    pass
        if isinstance(photo, list):
            if not photo:
                raise VKError("upload", 0, "ВК вернул пустой список фото")
            photo = photo[0]
        if isinstance(photo, dict):
            owner, photo_id = photo.get("owner_id"), photo.get("id")
            if owner is None or photo_id is None:
                raise VKError("upload", 0, "в ответе загрузки нет owner_id/id")
            return f"photo{owner}_{photo_id}"
        if isinstance(photo, str) and "_" in photo:
            owner, photo_id = photo.split("_")[:2]
            return f"photo{owner}_{photo_id}"
        raise VKError("upload", 0, f"непонятный ответ при загрузке фото: {photo!r}")

    async def upload_photos(
        self,
        session: aiohttp.ClientSession,
        photos: list[Path],
        peer_ids: list[int],
    ) -> str:
        """Загружает фото и собирает строку вложения для messages.send.

        Адрес загрузки ВК выдаёт на конкретную беседу, а в рассылке почти
        всегда есть беседы, куда сообществу доступа нет, поэтому цели
        перебираются по очереди до первого успеха.
        """
        last: Exception | None = None
        for peer_id in peer_ids or [0]:
            try:
                return await self._upload_to(session, photos, peer_id)
            except (VKError, OSError, aiohttp.ClientError) as err:
                log.info("Загрузка фото для peer_id %s не вышла: %s", peer_id, err)
                last = err
        if last is None:
            raise VKError("upload", 0, "некуда грузить фото: в рассылке нет ни одной беседы")
        raise last

    async def _upload_to(
        self, session: aiohttp.ClientSession, photos: list[Path], peer_id: int
    ) -> str:
        refs: list[str] = []
        for path in photos:
            # peer_id обязателен: ВК отдаёт адрес загрузки только для беседы,
            # в которую фото пойдёт, и без него отвечает отказом.
            server = await self._call(
                session, "photos.getMessagesUploadServer", peer_id=peer_id
            )
            upload_url = server.get("upload_url")
            if not upload_url:
                raise VKError("photos.getMessagesUploadServer", 0, "ВК не вернул upload_url")

            data = aiohttp.FormData()
            data.add_field("photo", path.read_bytes(), filename=path.name)
            try:
                async with session.post(upload_url, data=data) as resp:
                    status = resp.status
                    raw = await resp.text()
            except aiohttp.ClientConnectorCertificateError as err:
                raise VKError("upload", 0, TLS_HINT) from err
            try:
                uploaded = json.loads(raw)
            except json.JSONDecodeError as err:
                # Загрузчик вместо JSON иногда отдаёт HTML-страницу «страница
                # временно недоступна» с HTTP 500 — это его кратковременный сбой.
                raise VKError(
                    "upload",
                    0,
                    f"загрузчик ВК ответил HTTP {status} без JSON — это его "
                    f"кратковременный сбой, отправь ещё раз",
                ) from err
            if "error" in uploaded:
                err = uploaded["error"]
                if isinstance(err, dict):
                    raise VKError("upload", err.get("error_code", 0), err.get("error_msg", "ошибка загрузки"))
                raise VKError("upload", 0, str(err))

            refs.append(await self._save_photo(session, uploaded))
        return ",".join(refs)

    async def _save_photo(
        self, session: aiohttp.ClientSession, uploaded: dict
    ) -> str:
        """Превращает загруженный файл в photo{owner_id}_{photo_id}.

        Загрузчик отдаёт только kid и хеш, а идентификаторы фото появляются
        только после photos.saveMessagesPhoto — без него вложение не собрать.
        """
        missing = [key for key in ("server", "photo", "hash") if not uploaded.get(key)]
        if missing:
            raise VKError("upload", 0, f"в ответе загрузчика нет полей: {', '.join(missing)}")
        saved = await self._call(
            session,
            "photos.saveMessagesPhoto",
            server=uploaded["server"],
            photo=uploaded["photo"],
            hash=uploaded["hash"],
        )
        if isinstance(saved, dict):
            saved = [saved]
        if not saved:
            raise VKError("photos.saveMessagesPhoto", 0, "ВК не вернул сохранённое фото")
        return self._photo_ref(saved)


    async def send(
        self, text: str, targets: list[Target], photos: list[Path]
    ) -> list[SendResult]:
        results: list[SendResult] = []
        async with vk_session(timeout=60) as session:
            attachment = ""
            used = 0
            if photos:
                upload_peers = [
                    target.peer_id
                    for target in targets
                    if target.peer_id is not None
                    and not (self.mode == "community" and suspicious_peer_id(target.peer_id))
                ]
                try:
                    attachment = await self.upload_photos(
                        session, photos[:MAX_ATTACHMENTS], upload_peers
                    )
                    used = min(len(photos), MAX_ATTACHMENTS)
                except (VKError, OSError) as err:
                    log.exception("Не удалось загрузить фото в ВК")
                    detail = explain(err) if isinstance(err, VKError) else str(err)
                    return [SendResult(target=ch, ok=False, detail=f"загрузка фото: {detail}") for ch in targets]

            for index, target in enumerate(targets):
                if index:
                    await asyncio.sleep(SEND_DELAY_SECONDS)
                if target.peer_id is None:
                    results.append(SendResult(target, False, "не задан peer_id — допиши его в config.toml"))
                    continue
                if self.mode == "community" and suspicious_peer_id(target.peer_id):
                    results.append(
                        SendResult(
                            target,
                            False,
                            f"peer_id {target.peer_id} — это не беседа. Беседа начинается с "
                            f"{MULTICHAT_MIN_PEER_ID}. Похоже на id пользователя: сообщество не "
                            "пишет тем, кто ему не писал и не разрешил сообщения.",
                        )
                    )
                    continue
                try:
                    await self._call(
                        session,
                        "messages.send",
                        peer_id=target.peer_id,
                        message=text,
                        attachment=attachment or "",
                        random_id=random.randint(1, 2**31 - 1),
                    )
                    results.append(SendResult(target, True, "отправлено", attachments=used))
                except (VKError, aiohttp.ClientError) as err:
                    log.warning("Отправка в %s не удалась: %s", target.name, err)
                    results.append(SendResult(target, False, explain(err) if isinstance(err, VKError) else str(err)))
        return results