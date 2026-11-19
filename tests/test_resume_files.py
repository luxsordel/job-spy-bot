from __future__ import annotations

import pytest

from jobspybot.resume_files import (
    ResumeFileError,
    extract_text,
    redact_contacts,
    title_from_filename,
)


def make_pdf(text: str) -> bytes:
    """Одностраничный PDF со стандартным шрифтом Helvetica."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_extract_pdf():
    text = "Python developer with FastAPI and PostgreSQL experience, 2 years"
    assert extract_text(make_pdf(text), "CV.PDF") == text


def test_extract_txt_and_errors():
    assert extract_text(("строка  с   пробелами " * 5).encode(), "cv.txt").startswith(
        "строка с пробелами"
    )
    with pytest.raises(ResumeFileError):
        extract_text(b"x" * 100, "cv.docx")
    with pytest.raises(ResumeFileError):
        extract_text(b"short", "cv.txt")
    with pytest.raises(ResumeFileError):
        extract_text(b"not a pdf at all", "cv.pdf")


def test_title_and_redaction():
    assert title_from_filename("Резюме_Python (ИИ).pdf") == "Резюме Python (ИИ)"
    text = "Тел: +375 (29) 979-71-29, почта a.b@mail.com, опыт 2 года"
    assert redact_contacts(text) == "Тел: [телефон], почта [email], опыт 2 года"
