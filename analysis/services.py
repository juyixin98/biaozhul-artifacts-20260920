"""Upload orchestration: validate, extract, dedupe (per course), enqueue.

One document failing to extract never blocks the rest of the batch: each
file gets its own UploadAttempt row with a locate-able reason, and only the
accepted files become Documents + AnalysisTasks.
"""
from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction

from .extraction import (
    MAX_FILE_BYTES,
    SUPPORTED_EXTENSIONS,
    UploadError,
    content_digest,
    extract_text,
    normalise_text,
)
from .models import AnalysisTask, Document, SubmissionBatch, UploadAttempt


class BatchValidationError(Exception):
    """Whole-request rejection (e.g. >100 files or no files)."""


class AccessDenied(PermissionError):
    """Authenticated user is not a teacher of the target course."""


def _user_can_access_course(user, course) -> bool:
    if course.owner_id == user.id:
        return True
    return course.teachers.filter(pk=user.id).exists()


@transaction.atomic
def create_batch(user, course, files, note: str = "") -> SubmissionBatch:
    max_files = settings.ANALYSIS["MAX_BATCH_FILES"]
    max_bytes = settings.ANALYSIS["MAX_FILE_BYTES"]
    if not _user_can_access_course(user, course):
        raise AccessDenied("not a teacher of this course")
    if len(files) == 0:
        raise BatchValidationError("no files were provided")
    if len(files) > max_files:
        raise BatchValidationError(f"a batch accepts at most {max_files} files")

    batch = SubmissionBatch.objects.create(
        course=course, uploaded_by=user, note=note[:500]
    )
    # Digests accepted earlier in THIS batch count too, so uploading the same
    # file twice in one request reports the second slot as a duplicate.
    seen_digests: set[str] = set()

    for upload in files:
        filename = upload.name
        raw = upload.read()
        attempt = UploadAttempt(
            batch=batch, course=course, filename=filename, size_bytes=len(raw)
        )
        try:
            if len(raw) > max_bytes:
                raise UploadError(f"file exceeds {MAX_FILE_BYTES // (1024 * 1024)}MB limit")
            if not filename.lower().endswith(tuple(SUPPORTED_EXTENSIONS)):
                raise UploadError("only .txt and .docx files are accepted")

            text = extract_text(filename, raw, getattr(upload, "content_type", ""))
            normalised = normalise_text(text)
            digest = content_digest(normalised)
            attempt.content_sha256 = digest

            if digest in seen_digests:
                existing = Document.objects.filter(
                    course=course, content_sha256=digest
                ).first()
                _save_attempt(attempt, UploadAttempt.Status.DUPLICATE, existing,
                              "identical file already included in this batch")
                continue
            seen_digests.add(digest)

            existing = Document.objects.filter(
                course=course, content_sha256=digest
            ).first()
            if existing is not None:
                _save_attempt(attempt, UploadAttempt.Status.DUPLICATE, existing,
                              "same content already submitted to this course")
                continue

            word_count = len(normalised.split())
            try:
                with transaction.atomic():
                    document = Document.objects.create(
                        course=course,
                        batch=batch,
                        filename=filename,
                        content_type=getattr(upload, "content_type", "") or "",
                        size_bytes=len(raw),
                        content_sha256=digest,
                        text=normalised,
                        char_count=len(normalised),
                        word_count=word_count,
                        uploaded_by=user,
                    )
            except IntegrityError:
                # Concurrent upload of the same (course, digest) beat the
                # earlier existence check — treat it as a duplicate, never
                # crash the batch.
                existing = Document.objects.get(
                    course=course, content_sha256=digest
                )
                _save_attempt(attempt, UploadAttempt.Status.DUPLICATE, existing,
                              "same content submitted concurrently")
                continue
            _save_attempt(attempt, UploadAttempt.Status.CREATED, document,
                          "new document")
            AnalysisTask.objects.create(
                document=document,
                max_attempts=settings.QUEUE["MAX_ATTEMPTS"],
            )
        except UploadError as exc:
            # Rejected at the boundary: no document, no task, but the file
            # slot is recorded so the uploader can locate/fix it.
            _save_attempt(attempt, UploadAttempt.Status.REJECTED, None, str(exc))
        except Exception as exc:  # defensive: one bad file must not kill batch
            _save_attempt(
                attempt, UploadAttempt.Status.REJECTED, None,
                f"unexpected error: {type(exc).__name__}: {exc}",
            )

    return batch


def _save_attempt(attempt, status, document, detail):
    attempt.status = status
    attempt.document = document
    attempt.detail = detail[:500]
    attempt.save()


@transaction.atomic
def enqueue_rerun(user, document) -> AnalysisTask:
    """Create a fresh task for an existing document (new result version)."""
    if not _user_can_access_course(user, document.course):
        raise AccessDenied("not a teacher of this course")
    active = AnalysisTask.objects.filter(
        document=document,
        status__in=[AnalysisTask.Status.PENDING, AnalysisTask.Status.RUNNING],
    ).exists()
    if active:
        raise BatchValidationError("an analysis task for this document is already open")
    task = AnalysisTask.objects.create(
        document=document,
        max_attempts=settings.QUEUE["MAX_ATTEMPTS"],
    )
    return task
