from __future__ import annotations

import asyncio
import json
import logging
import re

import aiohttp

from .config import LLMConfig

log = logging.getLogger(__name__)

FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


class LLMError(RuntimeError):
    pass


def _clean(text: str) -> str:
    return FENCE.sub("", text).strip()


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
    ) -> str:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._config.temperature if temperature is None else temperature,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    body = await response.text()
                    if response.status >= 400:
                        raise LLMError(f"LLM вернул {response.status}: {body[:400]}")
            except aiohttp.ClientError as err:
                raise LLMError(
                    f"Не удалось достучаться до {url}: {err}. "
                    "Проверь, что провайдер ИИ запущен, а в настройках верные base_url и api_key."
                ) from err
            except TimeoutError as err:
                raise LLMError(
                    f"LLM не ответила за {self._config.timeout_seconds} с"
                ) from err

        try:
            data = json.loads(body)
        except ValueError as err:
            raise LLMError(f"Ответ LLM не является JSON: {body[:200]}") from err

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"Пустой ответ LLM: {body[:200]}")
        content = (choices[0].get("message") or {}).get("content") or ""
        if not content.strip():
            raise LLMError("Модель вернула пустое сообщение")
        return _clean(content)

    async def complete_many(
        self,
        prompts: list[str],
        *,
        temperature: float | None = None,
    ) -> list[str | Exception]:
        return await asyncio.gather(
            *(self.complete(p, temperature=temperature) for p in prompts),
            return_exceptions=True,
        )


async def list_models(config: LLMConfig, *, timeout_seconds: float | None = None) -> list[str]:
    """Список моделей провайдера — им же проверяется подключение в настройках."""
    url = f"{config.base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    limit = timeout_seconds if timeout_seconds is not None else min(config.timeout_seconds, 20)
    timeout = aiohttp.ClientTimeout(total=limit)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, headers=headers) as response:
                body = await response.text()
                if response.status >= 400:
                    raise LLMError(
                        f"{config.base_url} ответил {response.status}: {body[:200]}"
                    )
        except aiohttp.ClientError as err:
            raise LLMError(f"Не удалось достучаться до {url}: {err}") from err
        except TimeoutError as err:
            raise LLMError(f"{url} не ответил за {limit} с") from err

    try:
        data = json.loads(body)
    except ValueError as err:
        raise LLMError(f"{url} ответил не JSON: {body[:200]}") from err
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError(f"{url} не вернул список моделей: {body[:200]}")
    return sorted({str(i["id"]) for i in items if isinstance(i, dict) and i.get("id")})


async def probe(config: LLMConfig, *, timeout_seconds: float) -> None:
    """Проверка живости провайдера: поднимается LLMError, если достучаться нельзя."""
    await list_models(config, timeout_seconds=timeout_seconds)


def _is_local(base_url: str) -> bool:
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host.lower() in LOCAL_HOSTS


async def list_model_details(config: LLMConfig, *, timeout_seconds: float | None = None) -> list[dict]:
    """Модели провайдера с состоянием, если провайдер его отдаёт.

    LM Studio по OpenAI-совместимому /models отдаёт только то, что сейчас в памяти.
    Его нативный /api/v0/models перечисляет все скачанные модели и помечает state:
    loaded / not-loaded — их и хочет видеть пользователь в списке выбора.
    Для остальных провайдеров (и если нативного API нет) — обычный список без меток.
    """
    if _is_local(config.base_url):
        native = await _native_models(config, timeout_seconds)
        if native is not None:
            return native
    return [{"id": model_id, "state": "", "type": ""} for model_id in await list_models(config, timeout_seconds=timeout_seconds)]


async def _native_models(config: LLMConfig, timeout_seconds: float | None) -> list[dict] | None:
    # Нативный API живёт от корня хоста: у base_url вида http://host:1234/v1 префикс /v1
    # к нему не относится, и LM Studio ответит «не найдено».
    root = config.base_url.split("//", 1)[0] + "//" + config.base_url.split("//", 1)[-1].split("/", 1)[0]
    url = f"{root}/api/v0/models"
    limit = timeout_seconds if timeout_seconds is not None else min(config.timeout_seconds, 20)
    headers: dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=limit)) as session,
            session.get(url, headers=headers) as response,
        ):
            if response.status >= 400:
                return None
            body = await response.text()
        data = json.loads(body)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        log.debug("Нативный список моделей %s не ответил: %s", url, err)
        return None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [
        {
            "id": str(item.get("id") or ""),
            "state": str(item.get("state") or ""),
            "type": str(item.get("type") or ""),
        }
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]