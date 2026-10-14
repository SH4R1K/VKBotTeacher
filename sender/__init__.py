from __future__ import annotations

from core.config import AppConfig

from .base import Sender, SendResult, Target
from .clipboard import ClipboardSender
from .vk_api import (
    MULTICHAT_MIN_PEER_ID,
    VK_HINTS,
    VKError,
    VKSender,
    explain,
    suspicious_peer_id,
)

__all__ = [
    "MULTICHAT_MIN_PEER_ID",
    "VK_HINTS",
    "ClipboardSender",
    "SendResult",
    "Sender",
    "Target",
    "VKError",
    "VKSender",
    "build_sender",
    "explain",
    "suspicious_peer_id",
]


def build_sender(config: AppConfig) -> Sender:
    match config.transport.mode:
        case "user_token":
            return VKSender(config.vk.user_token, config.vk.api_version, mode="user_token")
        case "community":
            return VKSender(config.vk.community_token, config.vk.api_version, mode="community")
        case _:
            return ClipboardSender()