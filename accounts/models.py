"""Developer account -- the tenant principal."""
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class DeveloperManager(BaseUserManager):
    def create_user(self, username, email=None, password=None, **extra):
        if not username:
            raise ValueError("username is required")
        user = self.model(
            username=username,
            email=self.normalize_email(email or ""),
            **extra,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email=None, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(username, email, password, **extra)


class Developer(AbstractBaseUser, PermissionsMixin):
    """A tenant. Every App belongs to exactly one Developer."""

    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(blank=True, default="")
    company_name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_developer = models.BooleanField(
        default=True, help_text="Marker used by the IsDeveloper DRF permission."
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects = DeveloperManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["username"]

    def __str__(self) -> str:  # pragma: no cover
        return self.username
