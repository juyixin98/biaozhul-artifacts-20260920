from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework.authtoken.models import Token

from .models import Profile


@receiver(post_save, sender=get_user_model())
def ensure_profile_and_token(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)
        Token.objects.get_or_create(user=instance)
