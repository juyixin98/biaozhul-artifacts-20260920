"""Block until the database accepts connections and analysis tables exist.

Used by the worker container so it does not race the web container's
`manage.py migrate` on first boot.
"""
import time

from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, OperationalError, ProgrammingError, connections
from django.db.migrations.recorder import MigrationRecorder


class Command(BaseCommand):
    help = "Wait for the database and applied analysis migrations."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=120)
        parser.add_argument("--interval", type=float, default=1.0)

    def handle(self, *args, **options):
        deadline = time.monotonic() + options["timeout"]
        connection = connections[DEFAULT_DB_ALIAS]
        while True:
            try:
                connection.ensure_connection()
                recorder = MigrationRecorder(connection)
                applied = recorder.applied_migrations()
                if any(app == "analysis" for app, _name in applied):
                    self.stdout.write(self.style.SUCCESS("database is ready"))
                    return
            except (OperationalError, ProgrammingError):
                pass
            if time.monotonic() > deadline:
                raise SystemExit("database not ready in time")
            time.sleep(options["interval"])
