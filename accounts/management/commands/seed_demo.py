"""Seed a small demo dataset: project, crew, two templates (v1 + v2), users.

Run after ``docker compose up`` with::

    docker compose exec web python manage.py seed_demo
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import CrewMembership, Profile, ProjectAssignment, Role
from forms.models import Crew, FormTemplate, Project


class Command(BaseCommand):
    help = "Create demo project/crew/template/users (idempotent)."

    def handle(self, *args, **options):
        User = get_user_model()

        project, _ = Project.objects.get_or_create(
            name="Bridge Inspection",
            defaults={"description": "Routine field inspections, demo data"},
        )
        crew, _ = Crew.objects.get_or_create(project=project, name="Crew A")

        v1_fields = [
            {"key": "structure_id", "label": "Structure ID", "type": "text", "required": True},
            {
                "key": "surface",
                "label": "Surface type",
                "type": "enum",
                "options": ["concrete", "steel", "wood"],
                "required": True,
            },
            {
                "key": "crack_count",
                "label": "Crack count",
                "type": "number",
                "required_if": {"field": "surface", "op": "==", "value": "concrete"},
            },
            {"key": "inspection_date", "label": "Inspection date", "type": "date", "required": True},
            {"key": "notes", "label": "Notes", "type": "text"},
        ]
        v2_fields = v1_fields + [
            {"key": "gps_lat", "label": "Latitude", "type": "number"},
            {"key": "gps_lon", "label": "Longitude", "type": "number"},
        ]

        template, _ = FormTemplate.objects.get_or_create(
            project=project, code="deck-check", defaults={"name": "Deck condition check"}
        )
        if template.current_version_id is None:
            with transaction.atomic():
                v1 = template.publish_version(v1_fields, published_by=None)
                self.stdout.write(self.style.SUCCESS(f"Published {template.code} v{v1.version}"))
                v2 = template.publish_version(v2_fields, published_by=None)
                self.stdout.write(self.style.SUCCESS(f"Published {template.code} v{v2.version}"))

        supervisor, created = User.objects.get_or_create(username="supervisor1")
        if created:
            supervisor.set_password("supervisor12345")
            supervisor.save()
        Profile.objects.update_or_create(user=supervisor, defaults={"role": Role.SUPERVISOR})

        worker, created = User.objects.get_or_create(username="worker1")
        if created:
            worker.set_password("worker12345")
            worker.save()
        Profile.objects.update_or_create(user=worker, defaults={"role": Role.WORKER})

        for u in (supervisor, worker):
            ProjectAssignment.objects.get_or_create(user=u, project=project)
            CrewMembership.objects.get_or_create(user=u, crew=crew)

        self.stdout.write(
            self.style.SUCCESS(
                "Demo ready: admin/admin12345, supervisor1/supervisor12345, "
                "worker1/worker12345"
            )
        )
