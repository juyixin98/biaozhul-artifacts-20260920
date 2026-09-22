from django.apps import AppConfig
from django.db.backends.signals import connection_created


class SchedulerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scheduler"
    verbose_name = "MLOps GPU scheduler"

    def ready(self):
        # When running on the local SQLite backend, enable WAL journaling and
        # a generous busy timeout. This matters for the multi-scheduler
        # concurrency tests (several writer connections); on MySQL, which is
        # the production backend, FOR UPDATE row locks serialize writers and
        # these PRAGMAs are simply not issued.
        def _set_sqlite_pragma(sender, connection, **kwargs):
            if connection.vendor != "sqlite":
                return
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA busy_timeout=30000;")
                cursor.execute("PRAGMA synchronous=NORMAL;")

        connection_created.connect(_set_sqlite_pragma, weak=False, dispatch_uid="sqlite-wal")
