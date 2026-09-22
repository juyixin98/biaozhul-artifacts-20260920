from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers, status, viewsets
from rest_framework.response import Response

from accounts.access import accessible_project_ids, is_admin
from accounts.permissions import IsAdmin

from .models import Crew, FormTemplate, FormTemplateVersion, Project
from .serializers import (
    CrewSerializer,
    FormTemplateSerializer,
    ProjectSerializer,
    PublishVersionSerializer,
    TemplateVersionSerializer,
)


class _AdminWriteAuthenticatedRead:
    """Anyone authenticated may read; only admins may create/update/delete."""

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return super().get_permissions()
        return [IsAdmin()]


class ProjectViewSet(_AdminWriteAuthenticatedRead, viewsets.ModelViewSet):
    serializer_class = ProjectSerializer

    def get_queryset(self):
        return Project.objects.filter(id__in=accessible_project_ids(self.request.user))


class CrewViewSet(_AdminWriteAuthenticatedRead, viewsets.ModelViewSet):
    serializer_class = CrewSerializer

    def get_queryset(self):
        queryset = Crew.objects.select_related("project").filter(
            project_id__in=accessible_project_ids(self.request.user)
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


class FormTemplateViewSet(_AdminWriteAuthenticatedRead, viewsets.ModelViewSet):
    serializer_class = FormTemplateSerializer

    def get_queryset(self):
        queryset = FormTemplate.objects.select_related("project", "current_version").filter(
            project_id__in=accessible_project_ids(self.request.user)
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        code = self.request.query_params.get("code")
        if code:
            queryset = queryset.filter(code=code)
        return queryset


class TemplateVersionPublishView(generics.CreateAPIView):
    """POST /api/templates/{pk}/versions/  -- freeze a new immutable version."""

    permission_classes = (IsAdmin,)
    serializer_class = PublishVersionSerializer

    def create(self, request, pk):
        template = get_object_or_404(
            FormTemplate.objects.filter(
                id=pk, project_id__in=accessible_project_ids(request.user)
            )
        )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            version = template.publish_version(
                serializer.validated_data["fields"],
                published_by=request.user,
                min_supported_version=serializer.validated_data.get(
                    "min_supported_version"
                ),
            )
        return Response(
            TemplateVersionSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )


class TemplateVersionListView(generics.ListAPIView):
    serializer_class = TemplateVersionSerializer

    def get_queryset(self):
        template = get_object_or_404(
            FormTemplate.objects.filter(
                id=self.kwargs["pk"],
                project_id__in=accessible_project_ids(self.request.user),
            )
        )
        return template.versions.all()


class TemplateVersionDetailView(generics.RetrieveAPIView):
    """Fetch one immutable snapshot by template + version number.

    This is what offline clients download and pin before collecting data.
    """

    serializer_class = TemplateVersionSerializer

    def get_object(self):
        return get_object_or_404(
            FormTemplateVersion.objects.select_related("template").filter(
                template_id=self.kwargs["pk"],
                version=self.kwargs["version"],
                template__project_id__in=accessible_project_ids(self.request.user),
            )
        )
