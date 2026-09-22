"""Role and access-control models.

Project and Crew live in the ``forms`` app (they own form templates and
records); this app only holds the join tables describing who may reach them.
"""
from django.conf import settings
from django.db import models


class Role(models.TextChoices):
    ADMIN = "admin", "Administrator"
    SUPERVISOR = "supervisor", "Supervisor"
    WORKER = "worker", "Worker"


class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.WORKER)
    display_name = models.CharField(max_length=128, blank=True)

    def __str__(self):
        return f"{self.user.username}:{self.role}"


class ProjectAssignment(models.Model):
    """A worker/supervisor that is allowed to see a project's data."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_assignments",
    )
    project = models.ForeignKey(
        "forms.Project", on_delete=models.CASCADE, related_name="assignments"
    )

    class Meta:
        unique_together = ("user", "project")

    def __str__(self):
        return f"{self.user_id} -> project {self.project_id}"


class CrewMembership(models.Model):
    """Crews the user belongs to.

    Workers submit into these crews; supervisors resolve conflicts for them.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="crew_memberships",
    )
    crew = models.ForeignKey(
        "forms.Crew", on_delete=models.CASCADE, related_name="memberships"
    )

    class Meta:
        unique_together = ("user", "crew")

    def __str__(self):
        return f"{self.user_id} -> crew {self.crew_id}"
