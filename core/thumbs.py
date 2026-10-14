from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

THUMB_SIZE = 320
THUMB_DIRNAME = ".thumbs"
THUMB_SUFFIX = ".jpg"
THUMB_QUALITY = 80


class ThumbnailError(Exception):
    """Исходник не удалось превратить в миниатюру."""


def thumb_path(photos_dir: Path, name: str) -> Path:
    return photos_dir / THUMB_DIRNAME / (Path(name).stem + THUMB_SUFFIX)


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode not in ("RGBA", "LA", "P"):
        return image if image.mode == "RGB" else image.convert("RGB")
    # Прозрачность в JPEG не выразить — подкладываем белое поле.
    with_alpha = image.convert("RGBA")
    flat = Image.new("RGB", with_alpha.size, (255, 255, 255))
    flat.paste(with_alpha, mask=with_alpha.split()[-1])
    return flat


def make_thumb(source: Path, target: Path, size: int = THUMB_SIZE) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Имя через mkstemp: два параллельных запроса не должны писать в один файл.
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        with Image.open(source) as opened:
            oriented = ImageOps.exif_transpose(opened)
            image = _flatten(oriented if oriented is not None else opened)
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            image.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
        os.replace(tmp, target)
    except (OSError, ValueError, Image.UnidentifiedImageError) as err:
        tmp.unlink(missing_ok=True)
        raise ThumbnailError(f"{source.name}: {err}") from err