"""End-to-end pipeline + versioned results + rerun semantics."""
import pytest

from analysis import queue
from analysis.metrics import ALGORITHM_VERSION
from analysis.models import AnalysisResult, AnalysisTask, Document
from analysis.pipeline import process_task
from tests.conftest import upload

pytestmark = pytest.mark.django_db

LEASE = 60
MAX_ATTEMPTS = 3


def _drain():
    """Run every queued task once via the real pipeline."""
    outcomes = []
    while True:
        task = queue.claim_task(LEASE, MAX_ATTEMPTS)
        if task is None:
            return outcomes
        outcomes.append(process_task(task))


def test_full_pipeline_creates_versioned_result(api_a, course_a):
    upload(api_a, course_a, [("e.txt", (
        b"Mitosis is the process by which a single parent cell divides to "
        b"form two identical daughter cells. The chromosomes are copied in "
        b"interphase and then separated during anaphase. Each new cell "
        b"receives a complete set of genetic instructions."
    ))])
    outcomes = _drain()
    assert outcomes == ["succeeded"]
    doc = Document.objects.get()
    result = doc.results.get()
    assert result.version == 1
    assert result.algorithm_version == ALGORITHM_VERSION
    assert result.input_sha256 == doc.content_sha256
    assert result.task.status == AnalysisTask.Status.SUCCEEDED


def test_rerun_creates_new_version_without_overwriting(api_a, course_a):
    upload(api_a, course_a, [("e.txt", b"One two three four five six seven eight nine ten.")])
    _drain()
    doc = Document.objects.get()
    assert doc.results.count() == 1

    response = api_a.post(f"/api/documents/{doc.id}/rerun/")
    assert response.status_code == 201
    _drain()

    results = list(doc.results.order_by("version"))
    assert [r.version for r in results] == [1, 2]
    assert all(r.algorithm_version == ALGORITHM_VERSION for r in results)
    assert all(r.input_sha256 == doc.content_sha256 for r in results)
    # Results are immutable rows, not updated-in-place.
    assert results[0].id != results[1].id


def test_double_rerun_rejected_while_open(api_a, course_a):
    upload(api_a, course_a, [("e.txt", b"One two three four five six seven eight nine ten.")])
    _drain()
    doc = Document.objects.get()
    assert api_a.post(f"/api/documents/{doc.id}/rerun/").status_code == 201
    # A second rerun while the first task is pending is a conflict.
    response = api_a.post(f"/api/documents/{doc.id}/rerun/")
    assert response.status_code == 409


def test_progress_reported_per_batch(api_a, course_a):
    upload(api_a, course_a, [
        (f"f{i}.txt", f"Unique essay number {i} with plenty of distinct words "
                      f"about subject matter {i}.".encode())
        for i in range(4)
    ])
    batch_id = AnalysisTask.objects.first().document.batch_id
    _drain()
    resp = api_a.get(f"/api/batches/{batch_id}/")
    progress = resp.data["progress"]
    assert progress["documents"] == 4
    assert progress["succeeded"] == 4
    assert progress["failed"] == 0
    assert progress["percent"] == 100.0


def test_result_binding_fields(api_a, course_a):
    upload(api_a, course_a, [("e.txt", b"Short text.")])
    _drain()
    result = AnalysisResult.objects.get()
    payload = result.metrics_json
    assert payload["algorithm_version"] == ALGORITHM_VERSION
    assert "disclaimer" in payload
    assert "paragraphs" in payload and "lexical_richness" in payload
    assert "repeated_fragments" in payload and "style_similarity" in payload
