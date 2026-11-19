"""Извлечение текста резюме из загруженных файлов (PDF, TXT, MD)."""

from __future__ import annotations

import io
import re
from pathlib import PurePath

SUPPORTED = (".pdf", ".txt", ".md")
MAX_FILE_BYTES = 3_000_000
MAX_TEXT_CHARS = 8_000


class ResumeFileError(ValueError):
    pass


_SPACES = re.compile(r"[ \t ]{2,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")


def redact_contacts(text: str) -> str:
    """Убирает email и телефоны: для оценки вакансий они не нужны, а токены и приватность — да."""
    return _PHONE.sub("[телефон]", _EMAIL.sub("[email]", text))


def title_from_filename(file_name: str) -> str:
    stem = PurePath(file_name).stem.replace("_", " ").strip()
    return stem[:60] or "Резюме"


def extract_text(data: bytes, file_name: str) -> str:
    suffix = PurePath(file_name.lower()).suffix
    if suffix not in SUPPORTED:
        raise ResumeFileError("Поддерживаются PDF, .txt и .md")
    text = _pdf_text(data) if suffix == ".pdf" else data.decode("utf-8", "replace")
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.splitlines() if line.strip())
    if len(text) < 50:
        raise ResumeFileError(
            "Не удалось извлечь текст. Если PDF — это скан или картинка, пришлите текстом."
        )
    return text[:MAX_TEXT_CHARS]


def _pdf_text(data: bytes) -> str:
    # pypdf импортируется только при загрузке резюме — в обычной работе память не занимает
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:5])
    except PdfReadError as exc:
        raise ResumeFileError(f"Не удалось прочитать PDF: {exc}") from exc
