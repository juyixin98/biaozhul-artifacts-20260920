"""HTTP API. Every queryset is course-scoped to the requesting teacher."""
from __future__ import annotations

from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import (
    AnalysisResult,
    AnalysisTask,
    Course,
    Document,
    SubmissionBatch,
)
from .serializers import (
    BatchListSerializer,
    BatchSerializer,
    CourseSerializer,
    DocumentSerializer,
    ResultSerializer,
    TaskSerializer,
)


def teacher_courses(user):
    """Courses the user owns or is a member teacher of."""
    return Course.objects.filter(Q(owner=user) | Q(teachers=user)).distinct()


class CourseViewSet(viewsets.ModelViewSet):
    serializer_class = CourseSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return teacher_courses(self.request.user).prefetch_related("teachers")

    def perform_create(self, serializer):
        course = serializer.save(owner=self.request.user)
        course.teachers.add(self.request.user)

    @action(detail=True, methods=["post"])
    def add_teacher(self, request, pk=None):
        course = self.get_object()
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        try:
            other = user_model.objects.get(username=request.data.get("username"))
        except user_model.DoesNotExist:
            return Response({"detail": "user not found"},
                            status=status.HTTP_404_NOT_FOUND)
        course.teachers.add(other)
        return Response(CourseSerializer(course).data)


class BatchUploadView(APIView):
    """POST multipart/form-data: course + up to 100 `files`.

    Returns one UploadAttempt outcome per file slot (created/duplicate/
    rejected) so duplicate and rejected uploads stay visible.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        course_id = request.data.get("course")
        try:
            course = teacher_courses(request.user).get(pk=course_id)
        except (Course.DoesNotExist, ValueError, TypeError):
            return Response(
                {"detail": "course not found or not accessible"},
                status=status.HTTP_403_FORBIDDEN,
            )
        files = request.FILES.getlist("files")
        note = request.data.get("note", "")
        try:
            batch = services.create_batch(request.user, course, files, note)
        except services.BatchValidationError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(BatchSerializer(batch).data,
                        status=status.HTTP_201_CREATED)


class BatchViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = SubmissionBatch.objects.filter(
            course__in=teacher_courses(self.request.user)
        ).select_related("course", "uploaded_by")
        course_id = self.request.query_params.get("course")
        if course_id:
            qs = qs.filter(course_id=course_id)
        if self.action == "list":
            qs = qs.annotate(document_count=Count("documents", distinct=True))
        return qs.order_by("-created_at")

    def get_serializer_class(self):
        return BatchListSerializer if self.action == "list" else BatchSerializer


class DocumentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Document.objects.filter(
            course__in=teacher_courses(self.request.user)
        ).select_related("course", "batch")
        course_id = self.request.query_params.get("course")
        if course_id:
            qs = qs.filter(course_id=course_id)
        return qs.prefetch_related("results")

    @action(detail=True, methods=["post"], url_path="rerun")
    def rerun(self, request, pk=None):
        """Queue a new analysis run; always produces a new result version."""
        document = self.get_object()
        try:
            task = services.enqueue_rerun(request.user, document)
        except services.AccessDenied:
            return Response({"detail": "forbidden"},
                            status=status.HTTP_403_FORBIDDEN)
        except services.BatchValidationError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        return Response(TaskSerializer(task).data,
                        status=status.HTTP_201_CREATED)


class TaskViewSet(viewsets.ReadOnlyModelViewSet):
    """Progress/queue inspection, scoped to the teacher's courses."""

    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = AnalysisTask.objects.filter(
            document__course__in=teacher_courses(self.request.user)
        ).select_related("document")
        for key in ("status", "stage", "document"):
            value = self.request.query_params.get(key)
            if value is not None:
                qs = qs.filter(**{key: value})
        return qs.order_by("id")


class ResultViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ResultSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = AnalysisResult.objects.filter(
            document__course__in=teacher_courses(self.request.user)
        ).select_related("document")
        document_id = self.request.query_params.get("document")
        if document_id:
            qs = qs.filter(document_id=document_id)
        return qs
