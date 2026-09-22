"""重复上传去重 + 批量边界 + 单文件失败不阻断整批。"""

from django.test import TestCase

from engine.models import AnalysisTask, Submission
from engine.services import create_batch

from .factories import make_course, make_teacher, txt_upload


class DedupTests(TestCase):
    def setUp(self):
        from .factories import make_teacher

        self.teacher = make_teacher("teacher")
        self.course = make_course(self.teacher)

    def test_same_content_same_course_is_deduplicated(self):
        course = self.course
        text = "Same essay body. Second sentence here."
        f1 = txt_upload("a.txt", text)
        f2 = txt_upload("different-name.docx.txt", text)  # 文件名不同
        batch, summary = create_batch(
            course, self.teacher, [f1], note="b1"
        )
        self.assertEqual(summary["accepted"], 1)
        # 同课程再次上传相同内容
        batch2, summary2 = create_batch(
            course, self.teacher, [f2], note="b2"
        )
        self.assertEqual(summary2["duplicate"], 1)
        self.assertEqual(
            Submission.objects.filter(course=course,
                                      content_hash=summary["files"][0]["content_hash"]).count(),
            1,
        )
        self.assertEqual(AnalysisTask.objects.filter(submission__course=course).count(), 1)

    def test_same_content_different_courses_independent(self):
        """同文本在不同课程中：提交、权限、分析记录相互独立。"""
        t1 = make_teacher("t1")
        t2 = make_teacher("t2")
        c1 = make_course(t1, "C1")
        c2 = make_course(t2, "C2")
        text = "Identical body across courses. Another sentence follows."
        _, s1 = create_batch(c1, t1, [txt_upload("x.txt", text)])
        _, s2 = create_batch(c2, t2, [txt_upload("y.txt", text)])
        self.assertEqual(s1["files"][0]["content_hash"], s2["files"][0]["content_hash"])
        self.assertEqual(Submission.objects.count(), 2)
        self.assertNotEqual(s1["files"][0]["submission_id"],
                            s2["files"][0]["submission_id"])

    def test_corrupt_file_does_not_block_batch(self):
        course = self.course
        good = txt_upload("good.txt", "A perfectly fine document with several words.")
        bad = txt_upload("bad.docx", "this is not really a docx zip")
        # 手工把 .docx 的原始字节改为非法 zip
        bad.name = "bad.docx"
        batch, summary = create_batch(
            course, self.teacher, [good, bad]
        )
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["rejected"], 1)
        bad_item = next(f for f in summary["files"] if f["status"] == "rejected")
        self.assertEqual(bad_item["error_code"], "DOCX_PARSE_FAILED")

    def test_batch_limits(self):
        course = self.course
        from engine.services import FileValidationError

        with self.assertRaises(FileValidationError):
            create_batch(course, self.teacher, [])

        big = [txt_upload(f"f{i}.txt", f"text number {i}") for i in range(101)]
        with self.assertRaises(FileValidationError):
            create_batch(course, self.teacher, big)

    def test_file_size_limit(self):
        course = self.course
        big = txt_upload("big.txt", "x")
        big.size = 11 * 1024 * 1024
        batch, summary = create_batch(course, self.teacher, [big])
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["files"][0]["error_code"], "FILE_TOO_LARGE")

    def test_real_docx_roundtrip(self):
        """用 python-docx 生成的真实 DOCX 能正常入库并建立任务。"""
        import io

        from docx import Document

        buf = io.BytesIO()
        doc = Document()
        doc.add_paragraph("A real docx document with several words.")
        doc.add_paragraph("Second paragraph for the analysis engine.")
        doc.save(buf)

        class DocxUpload:
            name = "essay.docx"
            size = len(buf.getvalue())

            def __init__(self):
                self._buf = buf

            def read(self):
                return self._buf.getvalue()

        batch, summary = create_batch(
            self.course, self.teacher, [DocxUpload()], note="docx"
        )
        self.assertEqual(summary["accepted"], 1)
        sub = Submission.objects.get(id=summary["files"][0]["submission_id"])
        self.assertEqual(sub.source_format, Submission.SourceFormat.DOCX)
        self.assertEqual(AnalysisTask.objects.filter(submission=sub).count(), 1)
