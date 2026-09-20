"""Shared fixtures for the conformance test-suite."""

from django.contrib.auth.models import User
from django.utils import timezone

from conformance.models import (
    ProcessTemplate,
    Project,
    ProjectMembership,
    TemplateVersion,
)

# a -> b -> {c xor d} -> e, with time limits a->b (1h) and start->e (24h)
FLOW_DEFINITION = {
    "activities": ["a", "b", "c", "d", "e"],
    "dependencies": [
        {"from": "a", "to": "b"},
        {"from": "b", "to": "c"},
        {"from": "b", "to": "d"},
        {"any_of": ["c", "d"], "to": "e"},
    ],
    "exclusive_groups": [["c", "d"]],
    "time_limits": [
        {"from": "a", "to": "b", "max_seconds": 3600},
        {"from": None, "to": "e", "max_seconds": 86400},
    ],
}


def make_project(name="proj", username="analyst"):
    user = User.objects.create_user(username=username, password="pw12345")
    project = Project.objects.create(name=name)
    ProjectMembership.objects.create(user=user, project=project)
    return user, project


def make_template(project, definition=None, name="flow"):
    template = ProcessTemplate.objects.create(project=project, name=name)
    version = TemplateVersion.objects.create(
        template=template,
        version=1,
        definition=definition or FLOW_DEFINITION,
        status=TemplateVersion.Status.PUBLISHED,
        published_at=timezone.now(),
    )
    return template, version


def ev(event_id, case_key, activity, occurred_at, seq, version=None):
    """Build one import payload item."""
    item = {
        "event_id": event_id,
        "case_id": case_key,
        "activity": activity,
        "occurred_at": occurred_at,
        "seq": seq,
    }
    if version is not None:
        item["template_version_id"] = version.id
    return item
