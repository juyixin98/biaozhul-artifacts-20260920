"""
True concurrent-claim test against MySQL.

Skipped automatically on SQLite: SKIP LOCKED is a server-side row-lock
feature and the race it guards cannot be reproduced without it.

Run against the compose MySQL with:

    MYSQL_HOST=127.0.0.1 MYSQL_PASSWORD=textengine-pass \
        .venv/bin/pytest tests/test_mysql_concurrency.py -s

(The MySQL user needs rights to create the `test_textengine` database, or
pre-create it and grant privileges.)
"""
from datetime import timedelta
import threading

import pytest
from django.db import connection
from django.utils import timezone

from analysis import queue
from analysis.models import AnalysisTask
from tests.conftest import upload

requires_mysql = pytest.mark.skipif(
    connection.vendor != "mysql",
    reason="concurrent SKIP LOCKED claim requires MySQL",
)


@requires_mysql
@pytest.mark.django_db(transaction=True)
def test_two_workers_never_get_same_task(api_a, course_a):
    upload(
        api_a, course_a,
        [(f"f{i}.txt", f"Document {i} body with enough words.".encode())
         for i in range(20)],
    )
    claimed: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            while True:
                task = queue.claim_task(lease_seconds=30, max_attempts=3)
                if task is None:
                    return
                with lock:
                    claimed.append(task.id)
        except Exception as exc:  # pragma: no cover - failure diagnostic
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(claimed) == 20
    assert len(set(claimed)) == 20, "two workers claimed the same task id"


@requires_mysql
@pytest.mark.django_db(transaction=True)
def test_expired_task_is_reclaimed_exactly_once(api_a, course_a):
    upload(api_a, course_a, [("f.txt", b"Expired lease body text here.")])
    first = queue.claim_task(lease_seconds=1, max_attempts=3)
    AnalysisTask.objects.filter(pk=first.id).update(
        leased_at=timezone.now() - timedelta(seconds=10),
        lease_expires_at=timezone.now() - timedelta(seconds=9),
    )

    got: list[int] = []

    def worker():
        t = queue.claim_task(lease_seconds=30, max_attempts=3)
        if t:
            got.append(t.id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert got == [first.id]
