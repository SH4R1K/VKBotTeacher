from __future__ import annotations

import asyncio
import json
import logging
import re

import aiohttp

from .config import LLMConfig

log = logging.getLogger(__name__)

FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


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


async def list_models(config: LLMConfig) -> list[str]:
    """Список моделей провайдера — им же проверяется подключение в настройках."""
    url = f"{config.base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    timeout = aiohttp.ClientTimeout(total=min(config.timeout_seconds, 20))

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
            raise LLMError(f"{url} не ответил за {timeout.total} с") from err

    try:
        data = json.loads(body)
    except ValueError as err:
        raise LLMError(f"{url} ответил не JSON: {body[:200]}") from err
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError(f"{url} не вернул список моделей: {body[:200]}")
    return sorted({str(i["id"]) for i in items if isinstance(i, dict) and i.get("id")})