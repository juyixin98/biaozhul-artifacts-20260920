"""DOCX extraction + a genuinely corrupt upload."""
import io

import pytest

from analysis.extraction import UploadError, extract_text, normalise_text
from analysis.models import Document
from tests.conftest import upload

pytestmark = pytest.mark.django_db


def test_docx_roundtrip(api_a, course_a):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Field report", level=1)
    document.add_paragraph("First paragraph contains several observations.")
    document.add_paragraph("Second paragraph lists the follow-up actions.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Owner"
    table.rows[0].cells[1].text = "Reviewer"
    buffer = io.BytesIO()
    document.save(buffer)

    response = upload(api_a, course_a, [("report.docx", buffer.getvalue())])
    assert response.status_code == 201
    attempt = response.data["attempts"][0]
    assert attempt["status"] == "created"
    stored = Document.objects.get()
    assert "observations" in stored.text
    assert "Reviewer" in stored.text  # table content extracted too


def test_corrupt_docx_is_rejected_not_fatal(api_a, course_a):
    response = upload(api_a, course_a, [
        ("broken.docx", b"not really a zip archive"),
        ("ok.txt", b"A perfectly fine plain text companion file."),
    ])
    by_name = {a["filename"]: a for a in response.data["attempts"]}
    assert by_name["broken.docx"]["status"] == "rejected"
    assert "unreadable" in by_name["broken.docx"]["detail"]
    assert by_name["ok.txt"]["status"] == "created"


def test_extraction_rejects_unknown_type():
    with pytest.raises(UploadError):
        extract_text("sheet.xlsx", b"PK\x03\x04")


def test_normalisation_stable():
    a = normalise_text("Hello   world.\r\n\r\n\r\n\r\nSecond line.")
    b = normalise_text("Hello world.\n\n\nSecond line.")
    assert a == b
