from rest_framework import permissions

from .access import is_admin, is_supervisor


class IsAdmin(permissions.BasePermission):
    message = "Administrator role required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_admin(request.user))


class IsSupervisorOrAdmin(permissions.BasePermission):
    message = "Supervisor role required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_supervisor(request.user))
