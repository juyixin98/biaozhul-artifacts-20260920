"""Access-control helpers used across views and the sync service."""
from django.db.models import Q

from .models import Role


def get_role(user) -> str:
    if not user or not user.is_authenticated:
        return ""
    if user.is_superuser:
        return Role.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile else ""


def is_admin(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_role(user) == Role.ADMIN
    )


def is_supervisor(user) -> bool:
    return get_role(user) in (Role.ADMIN, Role.SUPERVISOR)


def accessible_project_ids(user):
    """Projects the user may read/write (admins implicitly see all)."""
    if is_admin(user):
        from forms.models import Project

        return Project.objects.values_list("id", flat=True)
    return user.project_assignments.values_list("project_id", flat=True)


def accessible_crew_ids(user):
    if is_admin(user):
        from forms.models import Crew

        return Crew.objects.values_list("id", flat=True)
    return user.crew_memberships.values_list("crew_id", flat=True)


def supervised_crew_ids(user):
    """Crews whose conflicts the user may resolve."""
    if is_admin(user):
        from forms.models import Crew

        return Crew.objects.values_list("id", flat=True)
    if get_role(user) == Role.SUPERVISOR:
        return user.crew_memberships.values_list("crew_id", flat=True)
    # An empty, always-false queryset.
    from forms.models import Crew

    return Crew.objects.none()


def can_access_project(user, project_id) -> bool:
    if is_admin(user):
        return True
    return user.project_assignments.filter(project_id=project_id).exists()


def can_submit_to_crew(user, crew_id) -> bool:
    """Only crew members submit records; admins can act on any crew."""
    if is_admin(user):
        return True
    return user.crew_memberships.filter(crew_id=crew_id).exists()


def scope_records(user, queryset):
    """Limit a FormRecord queryset to records the user may see."""
    if is_admin(user):
        return queryset
    return queryset.filter(
        Q(project_id__in=accessible_project_ids(user))
        & Q(crew_id__in=accessible_crew_ids(user))
    )
