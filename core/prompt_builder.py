from __future__ import annotations

from pathlib import Path

from .pdf_parser import Lesson, fallback_text

PLACEHOLDER = "{{ДАННЫЕ ЗАНЯТИЯ}}"


def load_style(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Файл стиля не найден: {path}")
    return path.read_text(encoding="utf-8")


def _lesson_block(lesson: Lesson) -> str:
    lines = [f"ТЕМА ЗАНЯТИЯ: {lesson.title}"]
    if lesson.summary:
        lines.append("")
        lines.append(f"РЕЗЮМЕ ЗАНЯТИЯ: {lesson.summary}")
    if lesson.outcomes:
        lines.append("")
        lines.append("ОБРАЗОВАТЕЛЬНЫЕ РЕЗУЛЬТАТЫ — чем ребята овладели:")
        lines.extend(f"- {item}" for item in lesson.outcomes)
    if lesson.stages:
        lines.append("")
        lines.append("ЗАДАЧИ ЭТАПОВ ЗАНЯТИЯ:")
        for stage in lesson.stages:
            lines.append(f"- [{stage.name}]")
            lines.extend(f"    · {task}" for task in stage.tasks)
    return "\n".join(lines)


def _fallback_block(pdf_path: Path) -> str:
    return (
        "Структура методички не распозналась, ниже полный текст занятия "
        "без служебных блоков. Сам выдели тему занятия, чем занимались ребята "
        "и итог занятия, затем составь сообщение по образцу выше.\n\n"
        + fallback_text(pdf_path)
    )


def build_prompt(style: str, lesson: Lesson, pdf_path: Path) -> str:
    data_block = _lesson_block(lesson) if lesson.is_usable else _fallback_block(pdf_path)
    if PLACEHOLDER in style:
        return style.replace(PLACEHOLDER, data_block)
    return f"{style.rstrip()}\n\n---\n\n{data_block}"