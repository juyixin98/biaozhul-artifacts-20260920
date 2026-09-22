"""端到端演示：教师 → 课程 → 样本 → 提交 → 执行 → 打印真实指标。

用法::

    python manage.py demo_analysis            # 自动建教师/课程并跑完整链路
    python manage.py demo_analysis --course X --username t
"""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from engine.analysis import sample_data
from engine.analysis.extract import content_hash_of
from engine.analysis.pipeline import feature_vector_for_sample
from engine.models import AnalysisResult, AnalysisTask, Course, StyleSample, Submission
from engine.worker import execute_task
from engine import queue


class Command(BaseCommand):
    help = "用固定样本文本演示一次完整分析，并打印真实指标 JSON。"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo_teacher")
        parser.add_argument("--course-id", type=int, default=None)
        parser.add_argument("--lease-seconds", type=int, default=60)

    @transaction.atomic
    def handle(self, *args, **opts):
        User = get_user_model()
        user, _ = User.objects.get_or_create(username=opts["username"])
        if opts["course_id"]:
            course = Course.objects.filter(id=opts["course_id"], teacher=user).first()
            if course is None:
                raise SystemExit("课程不存在或不属于该教师。")
        else:
            course, _ = Course.objects.get_or_create(
                name="演示课程：写作风格线索", teacher=user
            )

        # 样本
        for item in sample_data.STUDENT_SAMPLES:
            self._sample(course, StyleSample.Label.STUDENT, item)
        for item in sample_data.REFERENCE_SAMPLES:
            self._sample(course, StyleSample.Label.REFERENCE, item)

        # 目标提交（幂等：重复演示不会产生重复提交，但每次都会新建重跑任务/结果版本）
        text = sample_data.TARGET_TEXT
        h = content_hash_of(text)
        submission = Submission.objects.filter(course=course, content_hash=h).first()
        if submission is None:
            submission = Submission.objects.create(
                course=course, student_label="demo-student",
                filename=sample_data.TARGET_FILENAME,
                source_format=Submission.SourceFormat.TXT,
                raw_content=text.encode("utf-8"), content_hash=h,
                char_count=len(text),
            )

        task = AnalysisTask.objects.create(submission=submission)
        worker_id = queue.generate_worker_id()
        claimed = queue.claim_task(worker_id, opts["lease_seconds"])
        assert claimed is not None and claimed.id == task.id
        outcome = execute_task(claimed, worker_id, opts["lease_seconds"])
        self.stdout.write(f"任务结果：{outcome}")

        result = AnalysisResult.objects.filter(submission=submission).order_by("-version").first()
        self.stdout.write("=" * 70)
        self.stdout.write(
            f"submission_id={submission.id} result_version={result.version} "
            f"algorithm={result.algorithm_version} input_hash={result.input_hash[:16]}…"
        )
        self.stdout.write("=" * 70)
        self.stdout.write(json.dumps(result.metrics, ensure_ascii=False, indent=2))

    def _sample(self, course, label, item):
        h = content_hash_of(item["text"])
        if StyleSample.objects.filter(course=course, content_hash=h, label=label).exists():
            return
        vec = feature_vector_for_sample(item["text"])
        StyleSample.objects.create(
            course=course, label=label, name=item["name"],
            content_hash=h, text=item["text"],
            feature_version=vec["version"], feature_vector=vec,
        )
