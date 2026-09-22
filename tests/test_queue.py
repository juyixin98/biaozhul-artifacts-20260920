"""Queue protocol: claiming, leases, heartbeats, retries, stale fencing."""
from datetime import timedelta

import pytest
from django.utils import timezone

from analysis import queue
from analysis.models import AnalysisResult, AnalysisTask
from tests.conftest import upload

pytestmark = pytest.mark.django_db

LEASE = 5
MAX_ATTEMPTS = 3


def test_claim_is_fifo(api_a, course_a):
    upload(api_a, course_a, [(f"f{i}.txt", f"Document number {i} text body.".encode())
                             for i in range(3)])
    ids = list(AnalysisTask.objects.order_by("id").values_list("id", flat=True))
    claimed = [queue.claim_task(LEASE, MAX_ATTEMPTS).id for _ in range(3)]
    assert claimed == ids
    assert queue.claim_task(LEASE, MAX_ATTEMPTS) is None


def test_claim_bumps_generation_and_attempts(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    task = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert task.attempts == 1
    assert task.generation == 1
    assert task.worker_id
    assert task.status == AnalysisTask.Status.RUNNING
    assert task.lease_expires_at > timezone.now()


def test_active_lease_is_not_reclaimed(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    task = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert queue.claim_task(LEASE, MAX_ATTEMPTS) is None  # lease still valid
    # Heartbeat extends ownership.
    assert queue.heartbeat(task.id, task.worker_id, task.generation, LEASE)
    assert queue.claim_task(LEASE, MAX_ATTEMPTS) is None


def test_lease_expiry_allows_reclaim(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    old = queue.claim_task(LEASE, MAX_ATTEMPTS)
    old_worker = old.worker_id

    # Force the lease into the past.
    AnalysisTask.objects.filter(pk=old.pk).update(
        leased_at=timezone.now() - timedelta(seconds=LEASE + 10),
        lease_expires_at=timezone.now() - timedelta(seconds=10),
    )
    new = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert new.id == old.id
    assert new.worker_id != old_worker
    assert new.generation == 2
    assert new.attempts == 2


def test_zombie_worker_cannot_complete(api_a, course_a):
    """Old worker resumes after lease expiry -> completion refused."""
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    old = queue.claim_task(LEASE, MAX_ATTEMPTS)
    stale_worker, stale_gen = old.worker_id, old.generation

    AnalysisTask.objects.filter(pk=old.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    new = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert new.generation == stale_gen + 1

    def writer(task):
        raise AssertionError("stale worker must never reach the result writer")

    ok = queue.complete_task(old.id, stale_worker, stale_gen, writer)
    assert ok is False
    assert AnalysisResult.objects.count() == 0

    # The new generation can complete.
    def real_writer(task):
        AnalysisResult.objects.create(
            document=task.document, version=1, algorithm_version="1.0.0",
            input_sha256=task.document.content_sha256,
            metrics_json={"algorithm_version": "1.0.0"},
            style_vector=None, task=task,
        )

    assert queue.complete_task(new.id, new.worker_id, new.generation, real_writer)
    task = AnalysisTask.objects.get(pk=old.id)
    assert task.status == AnalysisTask.Status.SUCCEEDED
    assert task.worker_id == new.worker_id


def test_heartbeat_refused_after_loss(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    old = queue.claim_task(LEASE, MAX_ATTEMPTS)
    AnalysisTask.objects.filter(pk=old.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert queue.heartbeat(old.id, old.worker_id, old.generation, LEASE) is False


def test_failure_retries_then_fails(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    t1 = queue.claim_task(LEASE, MAX_ATTEMPTS)
    outcome = queue.fail_task(t1.id, t1.worker_id, t1.generation,
                              "metrics", "ValueError: boom", MAX_ATTEMPTS)
    assert outcome == "requeued"
    t1.refresh_from_db()
    assert t1.status == AnalysisTask.Status.PENDING
    assert t1.error_message == "ValueError: boom"
    assert t1.error_stage == "metrics"
    assert not t1.worker_id

    t2 = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert t2.id == t1.id and t2.attempts == 2
    queue.fail_task(t2.id, t2.worker_id, t2.generation, "parse", "again", MAX_ATTEMPTS)
    t3 = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert t3.attempts == 3
    outcome = queue.fail_task(t3.id, t3.worker_id, t3.generation,
                              "persist", "terminal", MAX_ATTEMPTS)
    assert outcome == "failed"
    t3.refresh_from_db()
    assert t3.status == AnalysisTask.Status.FAILED
    assert t3.attempts == MAX_ATTEMPTS
    # Stays dead: not claimable any more.
    assert queue.claim_task(LEASE, MAX_ATTEMPTS) is None


def test_stale_fail_report_ignored(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"First document text for analysis.")])
    old = queue.claim_task(LEASE, MAX_ATTEMPTS)
    AnalysisTask.objects.filter(pk=old.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1))
    new = queue.claim_task(LEASE, MAX_ATTEMPTS)
    outcome = queue.fail_task(old.id, old.worker_id, old.generation,
                              "metrics", "late error", MAX_ATTEMPTS)
    assert outcome == "stale"
    new.refresh_from_db()
    assert new.status == AnalysisTask.Status.RUNNING  # unaffected


def test_one_failed_document_does_not_block_batch(api_a, course_a):
    upload(api_a, course_a, [
        ("a.txt", b"First essay text with enough content in it."),
        ("b.txt", b"Second essay text discussing a different topic."),
    ])
    # Fail the first terminally.
    for _ in range(MAX_ATTEMPTS):
        t = queue.claim_task(LEASE, MAX_ATTEMPTS)
        queue.fail_task(t.id, t.worker_id, t.generation, "metrics", "x", MAX_ATTEMPTS)
    # The second document is still claimable and can succeed.
    t = queue.claim_task(LEASE, MAX_ATTEMPTS)
    assert t is not None
    assert queue.complete_task(
        t.id, t.worker_id, t.generation,
        lambda task: AnalysisResult.objects.create(
            document=task.document, version=1, algorithm_version="1.0.0",
            input_sha256=task.document.content_sha256,
            metrics_json={"algorithm_version": "1.0.0"},
            style_vector=None, task=task),
    )
