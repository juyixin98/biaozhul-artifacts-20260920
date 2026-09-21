from django.contrib import admin

from .models import AdEvent


@admin.register(AdEvent)
class AdEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_id",
        "event_type",
        "app",
        "placement",
        "network",
        "event_time",
        "revenue",
    )
    list_filter = ("event_type", "app", "network")
    search_fields = ("event_id",)
    date_hierarchy = "event_time"
