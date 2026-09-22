import json

from django.core.management.base import BaseCommand
from django.db import transaction

from engine.analysis import sample_data
from engine.analysis.extract import content_hash_of
from engine.analysis.pipeline import feature_vector_for_sample
from engine.models import Course, StyleSample


class Command(BaseCommand):
    help = "向指定课程写入固定风格样本（学生 4 份 + 参考范文 3 份）。"

    def add_arguments(self, parser):
        parser.add_argument("--course-id", type=int, required=True)

    @transaction.atomic
    def handle(self, *args, **opts):
        course = Course.objects.filter(id=opts["course_id"]).first()
        if course is None:
            raise SystemExit("课程不存在。")
        created = 0
        for item in sample_data.STUDENT_SAMPLES:
            created += self._add(course, StyleSample.Label.STUDENT, item)
        for item in sample_data.REFERENCE_SAMPLES:
            created += self._add(course, StyleSample.Label.REFERENCE, item)
        total = course.style_samples.count()
        self.stdout.write(self.style.SUCCESS(
            f"样本就绪：新增 {created} 份，课程内共 {total} 份。"))

    def _add(self, course, label, item):
        h = content_hash_of(item["text"])
        if StyleSample.objects.filter(course=course, content_hash=h, label=label).exists():
            return 0
        vec = feature_vector_for_sample(item["text"])
        StyleSample.objects.create(
            course=course, label=label, name=item["name"],
            content_hash=h, text=item["text"],
            feature_version=vec["version"], feature_vector=vec,
        )
        return 1
