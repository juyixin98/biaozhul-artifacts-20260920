"""Cross-course isolation: a teacher can never read another course's data."""
import pytest

from analysis.models import Document
from tests.conftest import upload

pytestmark = pytest.mark.django_db

TEXT = (
    b"Cell division proceeds through several well defined stages. "
    b"Mitosis preserves the chromosome number in the daughter cells."
)


def _seed(api_a, api_b, course_a, course_b):
    upload(api_a, course_a, [("a.txt", TEXT)])
    upload(api_b, course_b, [("b.txt", TEXT)])


def test_document_list_is_scoped(api_a, api_b, course_a, course_b):
    _seed(api_a, api_b, course_a, course_b)
    ids_a = {d["id"] for d in api_a.get("/api/documents/").data["results"]}
    ids_b = {d["id"] for d in api_b.get("/api/documents/").data["results"]}
    assert ids_a and ids_b and ids_a.isdisjoint(ids_b)


def test_document_detail_other_course_404(api_a, api_b, course_a, course_b):
    _seed(api_a, api_b, course_a, course_b)
    other_id = Document.objects.get(course=course_b).id
    response = api_a.get(f"/api/documents/{other_id}/")
    assert response.status_code == 404


def test_batches_and_tasks_and_results_scoped(api_a, api_b, course_a, course_b):
    from analysis import queue
    from analysis.pipeline import process_task

    _seed(api_a, api_b, course_a, course_b)
    # Drain both courses' queues so result rows exist and are also scoped.
    while (task := queue.claim_task(60, 3)) is not None:
        assert process_task(task) == "succeeded"

    for endpoint in ("/api/batches/", "/api/tasks/", "/api/results/"):
        data_a = api_a.get(endpoint).data
        data_b = api_b.get(endpoint).data
        assert isinstance(data_a, dict)  # paginated
        assert data_a["count"] == 1 and data_b["count"] == 1


def test_upload_to_foreign_course_forbidden(api_a, course_b):
    response = upload(api_a, course_b, [("x.txt", b"Attempt to sneak in.")])
    assert response.status_code == 403
    assert Document.objects.filter(course=course_b).count() == 0


def test_rerun_on_foreign_document_forbidden(api_a, api_b, course_a, course_b):
    _seed(api_a, api_b, course_a, course_b)
    other_id = Document.objects.get(course=course_b).id
    response = api_a.post(f"/api/documents/{other_id}/rerun/")
    assert response.status_code == 404  # hidden, not merely denied


def test_course_listing_excludes_foreign_courses(api_a, api_b, course_a, course_b):
    response = api_a.get("/api/courses/")
    ids = {c["id"] for c in response.data["results"]}
    assert course_a.id in ids and course_b.id not in ids


def test_anonymous_access_denied(api_anon):
    assert api_anon.get("/api/documents/").status_code == 401
    assert api_anon.get("/api/courses/").status_code == 401


def test_added_teacher_sees_course(api_a, api_b, course_a):
    # teacher_b gets added to course_a and can now read it.
    response = api_a.post(
        f"/api/courses/{course_a.id}/add_teacher/",
        {"username": "teacher_b"}, format="json",
    )
    assert response.status_code == 200
    response = api_b.get(f"/api/courses/{course_a.id}/")
    assert response.status_code == 200
