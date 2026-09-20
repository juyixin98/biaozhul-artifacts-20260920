from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from .models import Project


def get_authorized_project(request, project_id):
    """Return the project if the analyst is a member, else 404/403.

    Non-members get 404 so unauthorized project ids are not enumerable.
    """
    project = get_object_or_404(Project, pk=project_id)
    if request.user.is_staff:
        return project
    if not project.memberships.filter(user=request.user).exists():
        raise PermissionDenied("you are not a member of this project")
    return project
