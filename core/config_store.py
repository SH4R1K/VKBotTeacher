from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from core.config import LLM_PROVIDERS, ROOT, AppConfig

CONFIG_PATH = ROOT / "config.toml"


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render_config(config: AppConfig) -> str:
    lines = [
        "[llm]",
        f"provider = {_value(config.llm.provider)}",
        "# " + " | ".join(LLM_PROVIDERS),
        f"base_url = {_value(config.llm.base_url)}",
        f"api_key = {_value(config.llm.api_key)}",
        f"model = {_value(config.llm.model)}",
        f"temperature = {_value(config.llm.temperature)}",
        f"timeout_seconds = {config.llm.timeout_seconds}",
        f"max_tokens = {config.llm.max_tokens}",
        "",
        "[transport]",
        "# clipboard | user_token | community",
        f"mode = {_value(config.transport.mode)}",
        "",
        "[vk]",
        f"user_token = {_value(config.vk.user_token)}",
        f"community_token = {_value(config.vk.community_token)}",
        f"api_version = {_value(config.vk.api_version)}",
        "",
        "[server]",
        f"host = {_value(config.server.host)}",
        f"port = {config.server.port}",
        "",
        "[paths]",
        f"photos_dir = {_value(config.paths.photos_dir)}",
        f"history_dir = {_value(config.paths.history_dir)}",
        f"lessons_dir = {_value(config.paths.lessons_dir)}",
        f"style_file = {_value(config.paths.style_file)}",
        "",
        "# Чеклист бесед. peer_id нужен только для отправки через API,",
        "# в режиме clipboard он не используется.",
    ]
    for chat in config.chats:
        lines += ["", "[[chats]]", f"name = {_value(chat.name)}"]
        if chat.peer_id is not None:
            lines.append(f"peer_id = {chat.peer_id}")
    lines.append("")
    return "\n".join(lines)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = path or CONFIG_PATH
    text = render_config(config)
    # Не пишем файл, который сам же не читается обратно в ту же модель.
    if AppConfig.model_validate(tomllib.loads(text)) != config:
        raise ValueError("config.toml не пережил round-trip — запись отменена")
    if target.exists():
        shutil.copy2(target, target.with_name(f"{target.name}.bak"))
    temp = target.with_name(f"{target.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(target)
    return target