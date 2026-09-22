from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import CrewMembership, Profile, ProjectAssignment, Role
from forms.models import Crew, FormTemplate, Project


def make_user(username, role=Role.WORKER, password="pw12345678"):
    User = get_user_model()
    user = User.objects.create_user(username=username, password=password)
    # The post_save signal already creates a Workprofile; just set the role.
    Profile.objects.update_or_create(user=user, defaults={"role": role})
    return user


def api_for(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def assign(user, project=None, crew=None):
    if project is not None:
        ProjectAssignment.objects.create(user=user, project=project)
    if crew is not None:
        CrewMembership.objects.create(user=user, crew=crew)


def make_project_crew(name="Bridge", crew_name="Crew A"):
    project = Project.objects.create(name=name)
    crew = Crew.objects.create(project=project, name=crew_name)
    return project, crew


V1_FIELDS = [
    {"key": "structure_id", "label": "Structure ID", "type": "text", "required": True},
    {
        "key": "surface",
        "label": "Surface",
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
    {"key": "inspection_date", "label": "Date", "type": "date", "required": True},
    {"key": "notes", "label": "Notes", "type": "text"},
]


def make_template(project, code="deck-check", fields=None):
    return FormTemplate.objects.create(
        project=project, code=code, name="Deck check"
    )


def submit_entry(client, batch_id, entries):
    return client.post(
        "/api/sync/submit/",
        {"client_batch_id": batch_id, "entries": entries},
        format="json",
    )
