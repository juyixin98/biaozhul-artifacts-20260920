"""端到端 API：提交 → 进度 → 结果 → 重跑；自比样本排除；免责声明。"""

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from engine.analysis import sample_data
from engine.models import AnalysisResult, AnalysisTask
from engine.analysis.pipeline import feature_vector_for_sample
from engine.analysis.extract import content_hash_of

from .factories import make_course, make_teacher


class EndToEndTests(APITestCase):
    def setUp(self):
        self.teacher = make_teacher("t")
        self.course = make_course(self.teacher, "E2E")
        self.client.force_authenticate(self.teacher)

    def _seed_samples(self):
        for item in sample_data.STUDENT_SAMPLES + sample_data.REFERENCE_SAMPLES:
            text = item["text"]
            self.course.style_samples.create(
                label="student" if "student" in item["name"] else "reference",
                name=item["name"],
                content_hash=content_hash_of(text),
                text=text,
                feature_version="local-style-v1",
                feature_vector=feature_vector_for_sample(text),
            )

    def test_full_flow_with_progress_and_rerun(self):
        self._seed_samples()
        f = SimpleUploadedFile(
            sample_data.TARGET_FILENAME,
            sample_data.TARGET_TEXT.encode("utf-8"),
            content_type="text/plain",
        )
        resp = self.client.post(
            f"/api/courses/{self.course.id}/submit-batch/",
            {"files": [f], "note": "e2e"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        batch_id = resp.data["batch_id"]
        sid = resp.data["files"][0]["submission_id"]
        task_id = resp.data["files"][0]["task_id"]

        progress = self.client.get(
            f"/api/courses/{self.course.id}/batches/{batch_id}/"
        ).data
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["pending"], 1)

        # 执行任务（直接调用 worker 逻辑，避免依赖常驻进程）
        from engine import queue
        from engine.worker import execute_task

        claimed = queue.claim_task("test-worker", lease_seconds=300)
        self.assertEqual(claimed.id, task_id)
        self.assertEqual(execute_task(claimed, "test-worker", 300), "succeeded")

        progress = self.client.get(
            f"/api/courses/{self.course.id}/batches/{batch_id}/"
        ).data
        self.assertEqual(progress["succeeded"], 1)
        self.assertEqual(progress["overall_progress"], 100)

        detail = self.client.get(
            f"/api/courses/{self.course.id}/submissions/{sid}/"
        ).data
        self.assertEqual(detail["latest_result_version"], 1)
        metrics = detail["results"][0]["metrics"]
        # 植入的两处重复应被发现
        self.assertGreater(
            metrics["repeated_fragments"]["repeated_5gram_share"], 0.1
        )
        self.assertIn("disclaimer", metrics)
        # 样本数充足，相似度非空
        self.assertIsNotNone(metrics["style_similarity"])
        # 方法论含公式与版本
        self.assertIn("formulas", detail["results"][0]["methodology"])

        # 重跑 → 新版本
        rerun = self.client.post(
            f"/api/courses/{self.course.id}/submissions/{sid}/"
        ).data
        claimed2 = queue.claim_task("test-worker", lease_seconds=300)
        self.assertEqual(claimed2.id, rerun["task_id"])
        execute_task(claimed2, "test-worker", 300)
        detail2 = self.client.get(
            f"/api/courses/{self.course.id}/submissions/{sid}/"
        ).data
        self.assertEqual(detail2["latest_result_version"], 2)
        self.assertEqual(len(detail2["results"]), 2)

        # 任务查询接口
        task_resp = self.client.get(
            f"/api/courses/{self.course.id}/tasks/{task_id}/"
        )
        self.assertEqual(task_resp.status_code, 200)
        self.assertEqual(task_resp.data["status"], "SUCCEEDED")

    def test_self_matching_sample_is_excluded(self):
        """提交文本若与某样本完全相同（同 hash），该样本不参与相似度，
        但仍有其他 3+ 样本时正常输出。"""
        target = sample_data.TARGET_TEXT
        # 把目标文本本身也放成一个样本
        self.course.style_samples.create(
            label="student", name="self.txt",
            content_hash=content_hash_of(target), text=target,
            feature_version="local-style-v1",
            feature_vector=feature_vector_for_sample(target),
        )
        for item in sample_data.STUDENT_SAMPLES[:2] + sample_data.REFERENCE_SAMPLES[:2]:
            self.course.style_samples.create(
                label="student" if "student" in item["name"] else "reference",
                name=item["name"], content_hash=content_hash_of(item["text"]),
                text=item["text"], feature_version="local-style-v1",
                feature_vector=feature_vector_for_sample(item["text"]),
            )
        f = SimpleUploadedFile("self.txt", target.encode("utf-8"))
        resp = self.client.post(
            f"/api/courses/{self.course.id}/submit-batch/",
            {"files": [f]}, format="multipart",
        )
        sid = resp.data["files"][0]["submission_id"]
        from engine import queue
        from engine.worker import execute_task

        t = queue.claim_task("w", lease_seconds=300)
        execute_task(t, "w", 300)
        result = AnalysisResult.objects.get(submission_id=sid, version=1)
        # 5 份样本中 1 份自比被排除 → 参与 4 份，max 相似度 < 1
        self.assertEqual(result.metrics["style_eligible_sample_count"], 4)
        self.assertLess(result.metrics["style_similarity"]["max"], 0.999)
