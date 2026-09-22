"""Seed a demo course from samples/ and print the REAL computed metrics.

Idempotent: running twice creates the teacher/course once and uploads only
files whose content is not already present in the course. Every document is
analysed synchronously through the same pipeline the queue workers use, so
the numbers printed are exactly what the API would return.
"""
from __future__ import annotations

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from analysis import queue
from analysis.models import Course, Document
from analysis.pipeline import process_task
from analysis.services import create_batch
from analysis.extraction import SUPPORTED_EXTENSIONS

User = get_user_model()

DEMO_USERNAME = "demo_teacher"
DEMO_PASSWORD = "demo-password-123"
DEMO_COURSE = "Biology 101 (demo)"


class Command(BaseCommand):
    help = "Load samples/ into a demo course, run analysis and print metrics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--samples-dir",
            default=str(Path(__file__).resolve().parents[3] / "samples"),
        )

    def handle(self, *args, **options):
        user, _ = User.objects.get_or_create(
            username=DEMO_USERNAME,
            defaults={"is_staff": True},
        )
        user.set_password(DEMO_PASSWORD)
        user.save()
        course, _ = Course.objects.get_or_create(
            name=DEMO_COURSE, defaults={"owner": user}
        )
        course.teachers.add(user)

        sample_dir = Path(options["samples_dir"])
        files = []
        for path in sorted(sample_dir.iterdir()):
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(
                    SimpleUploaded(path.name, path.read_bytes())
                )
        if not files:
            self.stdout.write(self.style.WARNING(f"no samples found in {sample_dir}"))
            return

        batch = create_batch(user, course, files, note="seed demo")
        created = batch.attempts.filter(status="created").count()
        duplicate = batch.attempts.filter(status="duplicate").count()
        self.stdout.write(
            f"batch #{batch.id}: {created} new, {duplicate} duplicate, "
            f"{batch.attempts.filter(status='rejected').count()} rejected"
        )

        # Process this course's queued work synchronously (same pipeline the
        # queue workers run).
        doc_ids = list(Document.objects.filter(course=course).values_list("id", flat=True))
        while (task := queue.claim_task(
            lease_seconds=300, max_attempts=3, document_ids=doc_ids
        )) is not None:
            outcome = process_task(task)
            if outcome != "succeeded":
                self.stdout.write(self.style.ERROR(
                    f"task {task.id} -> {outcome}: {task.error_stage}: {task.error_message}"
                ))

        self._report(course)
        self.stdout.write(self.style.SUCCESS(
            f"\nDemo teacher login: {DEMO_USERNAME} / {DEMO_PASSWORD}"
        ))

    def _report(self, course):
        for doc in Document.objects.filter(course=course).order_by("filename"):
            result = doc.results.order_by("-version").first()
            if result is None:
                continue
            m = result.metrics_json
            p, r = m["paragraphs"], m["lexical_richness"]
            rep = m["repeated_fragments"]
            style = m["style_similarity"]
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"== {doc.filename} =="))
            self.stdout.write(
                f"  paragraphs={p['paragraph_count']}  sentences={p['sentence_count']}  "
                f"words={p['word_count']}"
            )
            wps = p["words_per_sentence"]
            self.stdout.write(
                f"  words/sentence mean={wps and wps['mean']} "
                f"stdev={wps and wps['stdev']}"
            )
            self.stdout.write(
                f"  TTR={r['type_token_ratio']}  MTLD(0.720)={r['mtld']}  "
                f"Honore={r['honore_stat']}  hapax={r['hapax_count']}"
                + ("  [small sample]" if r["small_sample"] else "")
            )
            self.stdout.write(
                f"  repeated-token-share={rep['repeated_token_share']}  "
                f"8-gram repeats={rep['per_ngram']['8']['repeated_distinct']}"
            )
            if style["status"] == "ok":
                top = style["matches"][0]
                self.stdout.write(
                    f"  style refs={style['references']} "
                    f"mean cosine={style['mean_cosine']} max={style['max_cosine']} "
                    f"(closest doc#{top['document_id']}={top['cosine']})"
                )
            else:
                self.stdout.write(
                    f"  style: insufficient samples "
                    f"({style['references']}/{style['required']} references)"
                )
            for w in m["warnings"]:
                self.stdout.write(self.style.WARNING(f"  warning: {w}"))


class SimpleUploaded:
    """Minimal stand-in for an uploaded file used by services.create_batch."""

    def __init__(self, name: str, content: bytes):
        self.name = name
        self.content = content
        self.content_type = "text/plain"
        self.size = len(content)

    def read(self) -> bytes:
        return self.content
