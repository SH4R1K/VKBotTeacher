from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf

PAGE_HEADER = re.compile(r"^\s*[Гг]ейм-дизайн\s+\S+\s+")
PAGE_NUMBER = re.compile(r"\s+\d{1,3}\s*$")

TITLE = re.compile(r"Методические указания\s+(?P<raw>.+?)\s*⚠")
CODE_TOKEN = re.compile(r"^[A-Za-zА-Яа-яЁё]{0,2}\d+[A-Za-zА-Яа-яЁё]{0,2}\d*$")

SECTION_EDUCATIONAL = "ОБРАЗОВАТЕЛЬНЫЕ РЕЗУЛЬТАТЫ УРОКА"
SECTION_STRUCTURE = "РЕКОМЕНДУЕМАЯ СТРУКТУРА УРОКА"
SECTION_SUMMARY = re.compile(r"⚠\s*РЕЗЮМЕ:\s*(?P<body>.+?)\s*(?=💾|$)")
NARRATIVE = re.compile(r"[^.!?\n]{0,90}\(\d{1,3}\s*мин\)")

STOP_MARKERS = (
    "💾 ССЫЛКИ И РЕКВИЗИТ",
    "⏩ ЗА ДЕНЬ ДО УРОКА",
    "🔖 ЗА 30 МИНУТ ДО УРОКА",
    "В НАЧАЛЕ УРОКА",
    "Инструкция",
    "НЕ ЗАБУДЬТЕ",
)

KNOWN_OUTCOMES = ("знают и понимают", "умеют")

STAGE_ROW = re.compile(
    r"(?P<time>\d{1,3})\s*мин\s+(?P<stage>[^❏]{1,140}?)\s*❏(?P<body>.*?)"
    r"(?=(?:\d{1,3}\s*мин\s+[^❏]{1,140}?\s*❏)|$)",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Stage:
    time_min: int
    name: str
    tasks: list[str]


@dataclass(slots=True)
class Lesson:
    code: str = ""
    title: str = ""
    summary: str = ""
    outcomes: list[str] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    has_structure: bool = False
    has_outcomes: bool = False

    @property
    def is_usable(self) -> bool:
        return bool(self.title) and (self.has_structure or self.has_outcomes)

    @property
    def tasks(self) -> list[str]:
        return [task for stage in self.stages for task in stage.tasks]


def _clean_page(raw: str) -> str:
    text = re.sub(r"[ \t]*\n[ \t]*", " ", raw)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = PAGE_HEADER.sub("", text, count=1)
    text = PAGE_NUMBER.sub("", text)
    return text.strip()


def read_pages(pdf_path: Path) -> list[str]:
    reader = pypdf.PdfReader(pdf_path)
    pages = [_clean_page(page.extract_text() or "") for page in reader.pages]
    if not pages:
        raise ValueError(f"PDF без извлекаемого текста: {pdf_path}")
    return pages


def _split_code(raw: str) -> tuple[str, str]:
    tokens = raw.split()
    code_parts: list[str] = []
    while tokens and CODE_TOKEN.match(tokens[0]):
        code_parts.append(tokens.pop(0))
    return " ".join(code_parts), " ".join(tokens).strip()


def _extract_title(text: str) -> tuple[str, str]:
    match = TITLE.search(text)
    if match:
        code, title = _split_code(match.group("raw"))
        if title:
            return code, title
    return "", ""


def _extract_title_from_metadata(pdf_path: Path) -> tuple[str, str]:
    try:
        meta = pypdf.PdfReader(pdf_path).metadata
    except Exception:  # noqa: BLE001 - метаданные необязательны, любой сбой = пустой заголовок
        return "", ""
    raw = (getattr(meta, "title", None) or "").strip()
    return _split_code(raw) if raw else ("", "")


def _extract_outcomes(section: str) -> list[str]:
    items: list[str] = []
    for label in KNOWN_OUTCOMES:
        anchor = re.search(rf"{label}:(?P<body>.*?)(?=●|$)", section, re.DOTALL)
        if anchor is None:
            continue
        collected: list[str] = []
        for match in re.finditer(r"[○●]\s*(?P<item>.+?)(?=(?:[○●])|$)", anchor.group("body"), re.DOTALL):
            text = match.group("item").strip(" ;.\n")
            if text:
                collected.append(text)
        items.extend(collected)
    return items


def _extract_stages(section: str) -> list[Stage]:
    stages: list[Stage] = []
    for row in STAGE_ROW.finditer(section):
        tasks = [t.strip(" ;.\n") for t in row.group("body").split("❏")]
        tasks = [t for t in tasks if len(t) > 3]
        if not tasks:
            continue
        name = re.sub(r"\s+", " ", row.group("stage")).strip(" :.")
        stages.append(Stage(time_min=int(row.group("time")), name=name, tasks=tasks))
    return stages


def _slice(text: str, start: int, end: int) -> str:
    return text[start:end].strip()


BOUNDARY_MARKERS = STOP_MARKERS + (SECTION_EDUCATIONAL, SECTION_STRUCTURE)


def _stop_sections_cut(text: str) -> list[str]:
    stops: list[tuple[int, bool]] = []
    for marker in BOUNDARY_MARKERS:
        is_stop = marker in STOP_MARKERS
        stops.extend((m.start(), is_stop) for m in re.finditer(re.escape(marker), text))
    if not any(is_stop for _, is_stop in stops):
        return [text]
    stops.sort()
    parts: list[str] = []
    cursor = 0
    for index, (start, is_stop) in enumerate(stops):
        if not is_stop:
            continue
        end = stops[index + 1][0] if index + 1 < len(stops) else len(text)
        parts.append(text[cursor:start])
        cursor = max(cursor, end)
    parts.append(text[cursor:])
    return [p for p in (chunk.strip() for chunk in parts) if p]


def parse_lesson(pdf_path: Path) -> Lesson:
    pages = read_pages(pdf_path)
    text = "\n\n".join(pages)

    code, title = _extract_title(text)
    if not title:
        code, title = _extract_title_from_metadata(pdf_path)
    if not title:
        title = _split_code(pdf_path.stem)[1]

    summary_match = SECTION_SUMMARY.search(text)
    summary = summary_match.group("body").strip() if summary_match else ""

    narrative = NARRATIVE.search(text)
    narrative_start = narrative.start() if narrative else len(text)

    structure_idx = text.find(SECTION_STRUCTURE)
    has_structure = structure_idx >= 0
    structure = (
        _slice(text, structure_idx + len(SECTION_STRUCTURE), narrative_start)
        if has_structure
        else ""
    )
    stages = _extract_stages(structure) if structure else []

    edu_idx = text.find(SECTION_EDUCATIONAL)
    edu_end = structure_idx if has_structure else narrative_start
    has_outcomes = edu_idx >= 0 and edu_end > edu_idx
    outcomes = (
        _extract_outcomes(_slice(text, edu_idx + len(SECTION_EDUCATIONAL), edu_end))
        if has_outcomes
        else []
    )

    return Lesson(
        code=code,
        title=title,
        summary=summary,
        outcomes=outcomes,
        stages=stages,
        has_structure=bool(stages),
        has_outcomes=bool(outcomes),
    )


def fallback_text(pdf_path: Path) -> str:
    """Полный текст занятия без служебных блоков — для режима «секции не нашлись»."""
    pages = read_pages(pdf_path)
    text = "\n\n".join(pages)
    narrative = NARRATIVE.search(text)
    if narrative:
        text = text[: narrative.start()]
    return "\n\n".join(_stop_sections_cut(text))