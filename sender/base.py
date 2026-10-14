from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    peer_id: int | None = None

    @property
    def is_api_capable(self) -> bool:
        return self.peer_id is not None


@dataclass(slots=True)
class SendResult:
    target: Target
    ok: bool
    detail: str
    attachments: int = 0


class Sender(Protocol):
    mode: str
    supports_photos: bool
    ready: bool

    async def send(
        self, text: str, targets: list[Target], photos: list[Path]
    ) -> list[SendResult]: ...