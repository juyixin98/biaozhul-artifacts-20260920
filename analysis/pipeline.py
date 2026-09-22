"""Worker-side task pipeline: run the metrics and fence every queue write."""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from .metrics import analyse_text
from .models import AnalysisResult, AnalysisTask
from . import queue

logger = logging.getLogger(__name__)


def collect_references(document):
    """Latest stored result of every OTHER document in the same course.

    Same-content rows are excluded by digest: comparing a document against
    itself (or an identical copy) would trivially yield cosine 1.
    """
    refs = []
    results = (
        AnalysisResult.objects.filter(document__course_id=document.course_id)
        .select_related("document")
        .order_by("document_id", "-version")
    )
    seen_docs: set[int] = set()
    for result in results:
        doc = result.document
        if doc.id == document.id:
            continue
        if doc.id in seen_docs:
            continue
        seen_docs.add(doc.id)
        if doc.content_sha256 == document.content_sha256:
            continue
        refs.append(
            {
                "document_id": doc.id,
                "input_sha256": result.input_sha256,
                "style_vector": result.style_vector,
            }
        )
    return refs


@transaction.atomic
def _store_result(task: AnalysisTask, metrics: dict, version: int) -> None:
    AnalysisResult.objects.create(
        document=task.document,
        version=version,
        algorithm_version=metrics["algorithm_version"],
        input_sha256=task.document.content_sha256,
        metrics_json=metrics,
        style_vector=metrics["style_vector"],
        task=task,
    )


def process_task(task: AnalysisTask) -> str:
    """Execute one claimed task. Returns 'succeeded' | 'failed' | 'requeued' |
    'stale'. Every guarded write is fenced by worker_id + generation."""
    cfg = settings.QUEUE
    analysis_cfg = settings.ANALYSIS
    worker_id = task.worker_id
    generation = task.generation
    task_id = task.id
    document = task.document

    try:
        if not queue.set_stage(
            task_id, worker_id, generation, AnalysisTask.Stage.PARSE
        ):
            return "stale"

        refs = collect_references(document)

        if not queue.set_stage(
            task_id, worker_id, generation, AnalysisTask.Stage.METRICS
        ):
            return "stale"
        metrics = analyse_text(
            document.text,
            references=refs,
            min_reference_samples=analysis_cfg["STYLE_MIN_REFERENCE_SAMPLES"],
        )

        if not queue.set_stage(
            task_id, worker_id, generation, AnalysisTask.Stage.STYLE
        ):
            return "stale"

        # Append-only version number, locked against concurrent reruns.
        with transaction.atomic():
            doc = (
                type(document).objects.select_for_update()
                .get(pk=document.pk)
            )
            last = doc.results.order_by("-version").values_list("version", flat=True).first()
            version = (last or 0) + 1

        if not queue.set_stage(
            task_id, worker_id, generation, AnalysisTask.Stage.PERSIST
        ):
            return "stale"

        ok = queue.complete_task(
            task_id, worker_id, generation,
            lambda t: _store_result(t, metrics, version),
        )
        return "succeeded" if ok else "stale"

    except Exception as exc:
        logger.exception("Task %s failed during processing", task_id)
        message = f"{type(exc).__name__}: {exc}"
        stage = task.stage
        # The traceback is intentionally not stored; keep PII-safe summary.
        del traceback
        outcome = queue.fail_task(
            task_id, worker_id, generation,
            stage=stage,
            message=message,
            max_attempts=cfg["MAX_ATTEMPTS"],
        )
        return outcome
