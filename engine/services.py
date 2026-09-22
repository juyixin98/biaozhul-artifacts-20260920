"""入库服务：校验、提取摘要、去重、建任务。与视图解耦便于复用与测试。"""

from django.db import IntegrityError, transaction

from .analysis.extract import ExtractionError, content_hash_of, extract
from .analysis.pipeline import feature_vector_for_sample
from .models import AnalysisTask, StyleSample, Submission, SubmissionBatch


class FileValidationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def detect_format(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".txt"):
        return Submission.SourceFormat.TXT
    if name.endswith(".docx"):
        return Submission.SourceFormat.DOCX
    raise FileValidationError("UNSUPPORTED_FORMAT", f"仅支持 .txt/.docx：{filename}")


def validate_upload(uploaded, max_files_exceeded=False):
    if uploaded.size == 0:
        raise FileValidationError("EMPTY_FILE", f"{uploaded.name} 为空文件。")
    if uploaded.size > 10 * 1024 * 1024:
        raise FileValidationError(
            "FILE_TOO_LARGE",
            f"{uploaded.name} 超过 10MB 上限（实际 {uploaded.size} 字节）。",
        )
    return detect_format(uploaded.name)


def create_sample(course, label, name, raw_text):
    """创建/返回课程内风格样本（同内容+同标签幂等）。"""
    from .analysis.extract import normalize_text

    text = normalize_text(raw_text)
    if not text:
        raise FileValidationError("EMPTY_TEXT", "样本文本为空。")
    h = content_hash_of(text)
    existing = StyleSample.objects.filter(
        course=course, content_hash=h, label=label
    ).first()
    if existing:
        return existing, False
    vector = feature_vector_for_sample(text)
    sample = StyleSample.objects.create(
        course=course,
        label=label,
        name=name,
        content_hash=h,
        text=text,
        feature_version=vector["version"],
        feature_vector=vector,
    )
    return sample, True


def ingest_file(course, batch, uploaded, student_label):
    """单文件入库。返回 dict 描述结果；单文件失败不影响同批其他文件。"""
    from django.conf import settings

    try:
        fmt = validate_upload(uploaded)
    except FileValidationError as exc:
        return {"filename": uploaded.name, "status": "rejected",
                "error_code": exc.code, "error": exc.message}

    raw = uploaded.read()
    if len(raw) > settings.FILE_MAX_BYTES:
        return {"filename": uploaded.name, "status": "rejected",
                "error_code": "FILE_TOO_LARGE",
                "error": f"{uploaded.name} 超过 10MB 上限。"}

    # 提取阶段提前做一次：坏文件立即得到可定位错误，不产生无意义任务。
    try:
        text = extract(fmt, raw)
    except ExtractionError as exc:
        return {"filename": uploaded.name, "status": "rejected",
                "error_code": exc.code, "error": exc.message}

    h = content_hash_of(text)
    with transaction.atomic():
        existing = (
            Submission.objects.select_for_update()
            .filter(course=course, content_hash=h)
            .first()
        )
        if existing is not None:
            # 同课程重复上传：幂等返回已有提交及其记录，不新建任务。
            return {
                "filename": uploaded.name,
                "status": "duplicate",
                "submission_id": existing.id,
                "content_hash": h,
                "message": "该内容已在本课程提交过，返回既有提交记录。",
            }
        try:
            submission = Submission.objects.create(
                course=course,
                batch=batch,
                student_label=student_label or "",
                filename=uploaded.name,
                source_format=fmt,
                raw_content=raw,
                content_hash=h,
                char_count=len(text),
            )
        except IntegrityError:
            # 并发重复提交竞态兜底（唯一约束 (course, content_hash)）
            dup = Submission.objects.get(course=course, content_hash=h)
            return {
                "filename": uploaded.name,
                "status": "duplicate",
                "submission_id": dup.id,
                "content_hash": h,
                "message": "并发提交检测到重复内容，复用既有记录。",
            }

        task = AnalysisTask.objects.create(submission=submission)
    return {
        "filename": uploaded.name,
        "status": "accepted",
        "submission_id": submission.id,
        "task_id": task.id,
        "content_hash": h,
        "char_count": len(text),
    }


def create_batch(course, teacher, files, note="", student_label=""):
    from django.conf import settings

    if not files:
        raise FileValidationError("NO_FILES", "未收到任何文件。")
    if len(files) > settings.BATCH_MAX_FILES:
        raise FileValidationError(
            "BATCH_TOO_LARGE",
            f"每批最多 {settings.BATCH_MAX_FILES} 份，本次 {len(files)} 份。",
        )

    batch = SubmissionBatch.objects.create(
        course=course, submitted_by=teacher, note=note
    )
    results = [ingest_file(course, batch, f, student_label) for f in files]
    summary = {
        "batch_id": batch.id,
        "accepted": sum(1 for r in results if r["status"] == "accepted"),
        "duplicate": sum(1 for r in results if r["status"] == "duplicate"),
        "rejected": sum(1 for r in results if r["status"] == "rejected"),
        "files": results,
    }
    return batch, summary


def enqueue_rerun(submission):
    """对已有提交发起重跑：新任务 → 新结果版本。历史版本全部保留。"""
    return AnalysisTask.objects.create(submission=submission)
