"""Duplicate-upload behaviour, incl. same text across courses."""
import pytest

from analysis.models import AnalysisTask, Document, UploadAttempt
from tests.conftest import upload

TEXT_A = (
    "Photosynthesis converts light energy into chemical energy. "
    "Plants absorb carbon dioxide and release oxygen during this process. "
    "The reactions happen inside chloroplasts, which contain chlorophyll."
).encode()

TEXT_B = b"A completely different essay about the history of railways."


@pytest.mark.django_db
def test_exact_duplicate_is_flagged_within_course(api_a, course_a):
    r1 = upload(api_a, course_a, [("essay.txt", TEXT_A)])
    assert r1.status_code == 201
    r2 = upload(api_a, course_a, [("essay_copy.txt", TEXT_A)])
    assert r2.status_code == 201

    # Only one document exists in the course, but both slots are recorded.
    assert Document.objects.filter(course=course_a).count() == 1
    attempts = UploadAttempt.objects.filter(course=course_a).order_by("id")
    assert attempts[0].status == UploadAttempt.Status.CREATED
    assert attempts[1].status == UploadAttempt.Status.DUPLICATE
    assert attempts[1].document_id == attempts[0].document_id
    # Duplicate must not create a second task.
    assert AnalysisTask.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_twice_in_one_batch(api_a, course_a):
    r = upload(api_a, course_a, [("a.txt", TEXT_A), ("b.txt", TEXT_A)])
    assert r.status_code == 201
    progress = r.data["progress"]
    assert progress["documents"] == 1
    statuses = sorted(a["status"] for a in r.data["attempts"])
    assert statuses == ["created", "duplicate"]


@pytest.mark.django_db
def test_same_text_in_different_courses_is_independent(api_a, api_b,
                                                       course_a, course_b):
    r1 = upload(api_a, course_a, [("essay.txt", TEXT_A)])
    r2 = upload(api_b, course_b, [("essay.txt", TEXT_A)])
    assert r1.status_code == 201 and r2.status_code == 201

    docs = Document.objects.filter(content_sha256=r1.data["attempts"][0]["content_sha256"])
    assert docs.count() == 2
    doc_a = docs.get(course=course_a)
    doc_b = docs.get(course=course_b)
    assert doc_a.id != doc_b.id
    # Independent submission records and independent queues.
    assert doc_a.attempts.count() == 1
    assert doc_b.attempts.count() == 1
    assert AnalysisTask.objects.filter(document=doc_a).count() == 1
    assert AnalysisTask.objects.filter(document=doc_b).count() == 1


@pytest.mark.django_db
def test_different_text_is_not_a_duplicate(api_a, course_a):
    upload(api_a, course_a, [("a.txt", TEXT_A)])
    r = upload(api_a, course_a, [("b.txt", TEXT_B)])
    assert r.status_code == 201
    assert Document.objects.filter(course=course_a).count() == 2
    assert AnalysisTask.objects.count() == 2


@pytest.mark.django_db
def test_whitespace_only_normalisation_dedupes(api_a, course_a):
    # Extra spaces/tabs and a UTF-8 BOM normalise to the same digest;
    # newlines are content lines, so they are not collapsed away.
    variant = (b"\xef\xbb\xbf"
               + TEXT_A.decode().replace(" ", "\t  ").encode())
    upload(api_a, course_a, [("a.txt", TEXT_A)])
    r = upload(api_a, course_a, [("a2.txt", variant)])
    assert r.data["attempts"][0]["status"] == "duplicate"


@pytest.mark.django_db
def test_batch_limits_and_rejections(api_a, course_a):
    # > 100 files is a whole-request rejection.
    too_many = [(f"f{i}.txt", b"some text content here") for i in range(101)]
    r = upload(api_a, course_a, too_many)
    assert r.status_code == 400

    # Bad extension + oversized file are recorded per slot, not fatal.
    big = b"x" * (10 * 1024 * 1024 + 1)
    r = upload(api_a, course_a, [
        ("ok.txt", TEXT_B),
        ("bad.pdf", b"%PDF-1.4"),
        ("big.txt", big),
    ])
    assert r.status_code == 201
    by_name = {a["filename"]: a for a in r.data["attempts"]}
    assert by_name["ok.txt"]["status"] == "created"
    assert by_name["bad.pdf"]["status"] == "rejected"
    assert by_name["big.txt"]["status"] == "rejected"
