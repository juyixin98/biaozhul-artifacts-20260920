"""并发与队列语义测试的共享工厂。"""

import io

from django.contrib.auth import get_user_model

from engine.models import AnalysisTask, Course, Submission


def make_teacher(username="teacher"):
    return get_user_model().objects.create_user(username=username, password="x")


def make_course(teacher=None, name="课程 A"):
    teacher = teacher or make_teacher()
    return Course.objects.create(name=name, teacher=teacher)


def make_submission(course, text="Hello world. This is a short text.", filename="a.txt",
                    fmt=Submission.SourceFormat.TXT, batch=None, label=""):
    from engine.analysis.extract import content_hash_of

    return Submission.objects.create(
        course=course, batch=batch, student_label=label, filename=filename,
        source_format=fmt, raw_content=text.encode("utf-8"),
        content_hash=content_hash_of(text), char_count=len(text),
    )


def make_task(submission):
    return AnalysisTask.objects.create(submission=submission)


def txt_upload(name, text):
    f = io.BytesIO(text.encode("utf-8"))
    f.name = name
    f.size = len(text.encode("utf-8"))
    return f
