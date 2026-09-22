from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.authtoken.models import Token

from .models import (
    AnalysisResult,
    AnalysisTask,
    Course,
    StyleSample,
    Submission,
    SubmissionBatch,
)
from .permissions import IsCourseTeacher
from .serializers import (
    BatchSerializer,
    CourseSerializer,
    ResultSerializer,
    StyleSampleSerializer,
    SubmissionSerializer,
    TaskSerializer,
)
from .services import FileValidationError, create_batch, create_sample, enqueue_rerun


def _owned_course(user, course_id):
    """只取属于当前教师的课程，否则 404（不暴露课程是否存在）。"""
    return get_object_or_404(Course, id=course_id, teacher=user)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def get_api_token(request):
    """为当前登录教师签发/取回 Token（便于演示与脚本调用）。"""
    token, _ = Token.objects.get_or_create(user=request.user)
    return Response({"token": token.key})


class CourseListCreate(generics.ListCreateAPIView):
    serializer_class = CourseSerializer

    def get_queryset(self):
        return Course.objects.filter(teacher=self.request.user)


class CourseDetail(generics.RetrieveDestroyAPIView):
    serializer_class = CourseSerializer
    permission_classes = [IsAuthenticated, IsCourseTeacher]

    def get_queryset(self):
        return Course.objects.filter(teacher=self.request.user)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def samples(request, course_id):
    course = _owned_course(request.user, course_id)
    if request.method == "GET":
        qs = course.style_samples.all().order_by("-id")
        data = StyleSampleSerializer(qs, many=True).data
        return Response({"count": len(data), "samples": data})

    label = request.data.get("label", StyleSample.Label.STUDENT)
    name = request.data.get("name", "")
    if "file" in request.FILES:
        uploaded = request.FILES["file"]
        from .analysis.extract import extract

        fmt = uploaded.name.lower().split(".")[-1]
        raw = uploaded.read()
        try:
            text = extract("docx" if fmt == "docx" else "txt", raw)
        except Exception as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    else:
        text = request.data.get("text", "")
        name = name or request.data.get("name", "inline-sample")
    try:
        sample, created = create_sample(course, label, name or uploaded.name, text)
    except FileValidationError as exc:
        return Response(
            {"error_code": exc.code, "error": exc.message},
            status=status.HTTP_400_BAD_REQUEST,
        )
    code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(StyleSampleSerializer(sample).data, status=code)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_batch(request, course_id):
    course = _owned_course(request.user, course_id)
    files = request.FILES.getlist("files")
    note = request.data.get("note", "")
    student_label = request.data.get("student_label", "")
    try:
        _, summary = create_batch(course, request.user, files, note, student_label)
    except FileValidationError as exc:
        return Response(
            {"error_code": exc.code, "error": exc.message},
            status=status.HTTP_400_BAD_REQUEST,
        )
    http_status = (
        status.HTTP_207_MULTI_STATUS
        if summary["rejected"] or summary["duplicate"]
        else status.HTTP_201_CREATED
    )
    return Response(summary, status=http_status)


def _batch_progress(batch):
    tasks = AnalysisTask.objects.filter(submission__batch=batch)
    agg = {s: 0 for s, _ in AnalysisTask.Status.choices}
    progresses = []
    for t in tasks.values("status", "progress"):
        agg[t["status"]] += 1
        progresses.append(t["progress"] or 0)
    total = len(progresses)
    pct = round(sum(progresses) / total) if total else 0
    return {
        "batch_id": batch.id,
        "total": total,
        "pending": agg[AnalysisTask.Status.PENDING],
        "running": agg[AnalysisTask.Status.RUNNING],
        "succeeded": agg[AnalysisTask.Status.SUCCEEDED],
        "failed": agg[AnalysisTask.Status.FAILED],
        "overall_progress": pct,
        "done": agg[AnalysisTask.Status.SUCCEEDED] + agg[AnalysisTask.Status.FAILED],
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def batch_status(request, course_id, batch_id):
    course = _owned_course(request.user, course_id)
    batch = get_object_or_404(SubmissionBatch, id=batch_id, course=course)
    return Response(_batch_progress(batch))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def task_status(request, course_id, task_id):
    course = _owned_course(request.user, course_id)
    task = get_object_or_404(AnalysisTask, id=task_id, submission__course=course)
    return Response(TaskSerializer(task).data)


class SubmissionList(generics.ListAPIView):
    serializer_class = SubmissionSerializer

    def get_queryset(self):
        course = _owned_course(self.request.user, self.kwargs["course_id"])
        return Submission.objects.filter(course=course).order_by("-id")


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def submission_detail(request, course_id, submission_id):
    course = _owned_course(request.user, course_id)
    submission = get_object_or_404(Submission, id=submission_id, course=course)

    if request.method == "POST":
        task = enqueue_rerun(submission)
        return Response(
            {"submission_id": submission.id, "task_id": task.id, "rerun": True},
            status=status.HTTP_201_CREATED,
        )

    data = SubmissionSerializer(submission).data
    results = submission.results.order_by("-version")
    data["results"] = ResultSerializer(results, many=True).data
    data["latest_result_version"] = results.first().version if results else None
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_detail(request, course_id, submission_id, version):
    course = _owned_course(request.user, course_id)
    result = get_object_or_404(
        AnalysisResult,
        submission_id=submission_id,
        submission__course=course,
        version=version,
    )
    return Response(ResultSerializer(result).data)
