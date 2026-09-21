from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """Object-level check: request.user must own the resource.

    Works for App (``owner``) and any model exposing ``app.owner``
    (Placement, PlacementNetwork, ConfigVersion, Experiment, AuditLog).
    """

    message = "You do not have access to this resource."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        owner = getattr(obj, "owner", None)
        if owner is None:
            app = getattr(obj, "app", None)
            owner = getattr(app, "owner", None)
        if owner is None:
            # e.g. Experiment -> placement -> app -> owner
            placement = getattr(obj, "placement", None)
            app = getattr(placement, "app", None)
            owner = getattr(app, "owner", None)
        return owner is not None and owner == request.user
