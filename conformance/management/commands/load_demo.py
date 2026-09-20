"""Load the demo project, template and event log.

Creates:
  * users  analyst/analyst123 (project member) and outsider/outsider123
  * project "demo-mining" with the "order-flow" template (published v1)
  * the demo event log from demo/events.json, imported in two batches so
    the second batch demonstrates late-arrival backfill (CASE-1002)

Idempotent: re-running skips everything that already exists.
"""

import json
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from conformance.models import (
    ProcessTemplate,
    Project,
    ProjectMembership,
    TemplateVersion,
)
from conformance.services import import_events
from conformance.template_def import validate_definition

DEMO_FILE = Path(__file__).resolve().parents[3] / "demo" / "events.json"


class Command(BaseCommand):
    help = "Load the MineRite demo project, template and event log"

    def handle(self, *args, **options):
        data = json.loads(DEMO_FILE.read_text())

        analyst, _ = User.objects.get_or_create(username="analyst")
        analyst.set_password("analyst123")
        analyst.save()
        outsider, _ = User.objects.get_or_create(username="outsider")
        outsider.set_password("outsider123")
        outsider.save()

        project, _ = Project.objects.get_or_create(name="demo-mining")
        ProjectMembership.objects.get_or_create(
            user=analyst, project=project,
            defaults={"role": ProjectMembership.Role.ANALYST},
        )

        tpl_def = data["template"]
        template, _ = ProcessTemplate.objects.get_or_create(
            project=project, name=tpl_def["name"]
        )
        version = template.versions.filter(version=1).first()
        if version is None:
            version = TemplateVersion.objects.create(
                template=template,
                version=1,
                definition=validate_definition(tpl_def["definition"]),
            )
        if version.status != TemplateVersion.Status.PUBLISHED:
            version.status = TemplateVersion.Status.PUBLISHED
            version.published_at = timezone.now()
            version.save()
        self.stdout.write(f"template '{template.name}' v{version.version} published")

        for i, batch in enumerate(data["batches"], start=1):
            items = [
                {"template_version_id": version.id, **item} for item in batch
            ]
            result = import_events(project, items)
            self.stdout.write(
                f"batch {i}: inserted={result.inserted} skipped={result.skipped}"
            )

        for case in project.cases.order_by("case_key"):
            current = case.analyses.filter(is_current=True).first()
            if current:
                self.stdout.write(
                    f"  {case.case_key}: {current.status} "
                    f"(deviations={len(current.deviations)}, "
                    f"missing_seqs={current.missing_seqs})"
                )
        self.stdout.write(self.style.SUCCESS("demo data loaded"))
