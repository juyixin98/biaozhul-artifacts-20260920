"""DRF permission / authentication helpers implementing tenant isolation.

Rule: a developer may only access resources owned by *their own* applications.
Ownership chain is always ``Developer -> App -> (Placement / ...)``.
"""
from rest_framework import permissions
from rest_framework.authentication import TokenAuthentication

from applications.models import App


class DeveloperTokenAuthentication(TokenAuthentication):
    """Token auth for the management API; keyword stays as default "Token"."""


class AppApiKeyAuthentication(TokenAuthentication):
    """Authentication for SDK / ingestion endpoints.

    The SDK authenticates with the application's API key. We resolve the key to
    an :class:`App` and return an ``AppAuthPrincipal`` so views can scope by app.
    """

    keyword = "ApiKey"

    def authenticate_credentials(self, key):
        try:
            app = App.objects.select_related("developer").get(api_key=key)
        except App.DoesNotExist:
            return None
        principal = AppAuthPrincipal(app=app)
        return principal, key


class AppAuthPrincipal:
    """Lightweight non-Django 'user' object attached to ``request.user``."""

    is_authenticated = True

    def __init__(self, *, app: App):
        self.app = app
        self.developer = app.developer

    def __str__(self) -> str:  # pragma: no cover
        return f"AppAuthPrincipal(app={self.app.public_id})"


class IsDeveloper(permissions.BasePermission):
    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and getattr(user, "is_developer", False))


class IsOwnerDeveloper(permissions.BasePermission):
    """Object-level check: ``obj.developer_id == request.user.id``.

    Also understands the common intermediate ``App`` (checks ``obj.app``).
    """

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        owner = getattr(obj, "developer", None) or getattr(getattr(obj, "app", None), "developer", None)
        if owner is None:
            return False
        return owner.pk == user.pk
