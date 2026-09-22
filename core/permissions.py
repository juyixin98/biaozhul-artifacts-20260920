"""自定义权限：工作人员仅访问分配项目；主管仅处理所属班组冲突。"""
from rest_framework.permissions import BasePermission

from .models import Profile, Project


def user_project_ids(user):
    """该用户可见的项目 ID 集合。

    * 管理员/staff：全部项目；
    * 主管：所属班组关联项目 ∪ 显式分配项目；
    * 工作人员：仅显式分配项目。
    """
    if not user.is_authenticated:
        return set()
    is_admin = user.is_staff or (
        getattr(user, "profile", None) and user.profile.role == Profile.Role.ADMIN
    )
    if is_admin:
        return set(Project.objects.values_list("id", flat=True))
    ids = set(user.profile.assigned_projects.values_list("id", flat=True))
    ids |= set(
        Project.objects.filter(teams__supervisors=user).values_list("id", flat=True)
    )
    return ids


def can_access_project(user, project):
    if not user.is_authenticated:
        return False
    if user.is_staff or user.profile.role == Profile.Role.ADMIN:
        return True
    return project.id in user_project_ids(user)


def can_manage_templates(user, project=None):
    """管理员可管理所有模板；主管只能管理所属班组关联项目的模板。"""
    if not user.is_authenticated:
        return False
    if user.is_staff or user.profile.role == Profile.Role.ADMIN:
        return True
    if user.profile.role != Profile.Role.SUPERVISOR:
        return False
    team_project_ids = set(
        Project.objects.filter(teams__supervisors=user).values_list("id", flat=True)
    )
    return project is None or project.id in team_project_ids


def can_resolve_project(user, project):
    """主管仅能处理所属班组（关联该项目）的冲突。"""
    if not user.is_authenticated:
        return False
    if user.is_staff or user.profile.role == Profile.Role.ADMIN:
        return True
    return (
        user.profile.role == Profile.Role.SUPERVISOR
        and Project.objects.filter(
            teams__supervisors=user, id=project.id
        ).exists()
    )


class IsAdminOrSupervisor(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.is_staff or u.profile.role in {
            Profile.Role.ADMIN,
            Profile.Role.SUPERVISOR,
        }


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and (u.is_staff or u.profile.role == Profile.Role.ADMIN)
        )
