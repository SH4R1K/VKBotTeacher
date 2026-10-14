from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent

TRANSPORT_MODES = ("clipboard", "user_token", "community")

# Все провайдеры говорят на OpenAI-совместимом /chat/completions, различаются адресом.
LLM_PROVIDERS: dict[str, dict[str, object]] = {
    "lm_studio": {
        "label": "LM Studio — локально",
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "needs_key": False,
    },
    "ollama": {
        "label": "Ollama — локально",
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "needs_key": False,
    },
    "openai": {
        "label": "OpenAI API",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "needs_key": True,
    },
    "gigachat": {
        "label": "GigaChat",
        "base_url": "https://api.gigachat.dev/v1",
        "api_key": "",
        "needs_key": True,
    },
    "custom": {
        "label": "Другой — свой адрес",
        "base_url": "",
        "api_key": "",
        "needs_key": True,
    },
}


class LLMConfig(BaseModel):
    provider: str = Field(
        default="lm_studio", pattern="^(" + "|".join(LLM_PROVIDERS) + ")$"
    )
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    model: str = "gemma-3-12b-it"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    max_tokens: int = Field(default=1200, ge=1)


class VKConfig(BaseModel):
    user_token: str = ""
    community_token: str = ""
    api_version: str = "5.199"


class TransportConfig(BaseModel):
    mode: str = Field(
        default="clipboard", pattern="^(" + "|".join(TRANSPORT_MODES) + ")$"
    )


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class PathsConfig(BaseModel):
    photos_dir: str = "photos"
    history_dir: str = "history"
    lessons_dir: str = "lessons"
    style_file: str = "style.md"


class Chat(BaseModel):
    name: str
    peer_id: int | None = None


class AppConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    transport: TransportConfig = TransportConfig()
    vk: VKConfig = VKConfig()
    server: ServerConfig = ServerConfig()
    paths: PathsConfig = PathsConfig()
    chats: list[Chat] = Field(default_factory=list)


def load_config(path: Path | None = None) -> AppConfig:
    import tomllib

    config_path = path or (ROOT / "config.toml")
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as fh:
        return AppConfig.model_validate(tomllib.load(fh))


def resolve_dir(config: AppConfig, name: str) -> Path:
    target = ROOT / name
    target.mkdir(parents=True, exist_ok=True)
    return target