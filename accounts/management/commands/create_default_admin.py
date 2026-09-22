from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.conf import settings

from accounts.models import Profile, Role


class Command(BaseCommand):
    help = "Create the bootstrap administrator account (idempotent)."

    def handle(self, *args, **options):
        username = settings.DEFAULT_ADMIN_USERNAME
        password = settings.DEFAULT_ADMIN_PASSWORD
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"is_staff": True, "is_superuser": True, "email": "admin@fieldsnap.local"},
        )
        if created:
            user.set_password(password)
            user.save(update_fields=["password"])
            Profile.objects.update_or_create(user=user, defaults={"role": Role.ADMIN})
            self.stdout.write(
                self.style.SUCCESS(f"Created admin {username!r} (password from DEFAULT_ADMIN_PASSWORD)")
            )
        else:
            # Ensure flags/role stay correct across restarts.
            changed = False
            if not user.is_superuser or not user.is_staff:
                user.is_superuser = True
                user.is_staff = True
                changed = True
            if changed:
                user.save(update_fields=("is_superuser", "is_staff"))
            Profile.objects.update_or_create(user=user, defaults={"role": Role.ADMIN})
            self.stdout.write(f"Admin {username!r} already exists")
