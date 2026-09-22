"""版本重跑：结果绑定输入摘要与算法版本；重跑生成新版本，历史保留。"""

from django.test import TransactionTestCase

from engine import queue
from engine.analysis.version import ALGORITHM_VERSION
from engine.models import AnalysisResult
from engine.worker import execute_task

from .factories import make_course, make_submission, make_task


class RerunVersionTests(TransactionTestCase):
    def _run_once(self, submission, worker):
        task = make_task(submission)
        claimed = queue.claim_task(worker, lease_seconds=300)
        self.assertEqual(claimed.id, task.id)
        outcome = execute_task(claimed, worker, lease_seconds=300)
        self.assertEqual(outcome, "succeeded")
        return task

    def test_rerun_creates_new_version_keeping_history(self):
        course = make_course()
        submission = make_submission(
            course,
            text="First version of the essay with several distinct words here.",
        )
        self._run_once(submission, "w1")
        self._run_once(submission, "w2")
        results = list(AnalysisResult.objects.filter(submission=submission).order_by("version"))
        self.assertEqual([r.version for r in results], [1, 2])
        self.assertEqual(results[0].input_hash, results[1].input_hash)
        self.assertEqual(results[0].algorithm_version, ALGORITHM_VERSION)

    def test_result_bound_to_input_hash_changes_with_content(self):
        course = make_course()
        submission = make_submission(course, text="Original body text for analysis.")
        self._run_once(submission, "w1")

        # 模拟“输入变化后重跑”：提交内容与内容摘要更新，新版本绑定新摘要
        from engine.analysis.extract import content_hash_of

        new_text = "Completely rewritten body with different vocabulary and style."
        submission.raw_content = new_text.encode("utf-8")
        submission.content_hash = content_hash_of(new_text)
        submission.char_count = len(new_text)
        submission.save()
        self._run_once(submission, "w2")

        results = list(AnalysisResult.objects.filter(submission=submission).order_by("version"))
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0].input_hash, results[1].input_hash)
        self.assertEqual(results[1].input_hash, content_hash_of(new_text))
        # 每个结果只对应一个任务，且任务不被复用
        self.assertEqual(len({r.task_id for r in results}), 2)
