from django.contrib import admin

from .models import NetworkScore, ScoreRun


class NetworkScoreInline(admin.TabularInline):
    model = NetworkScore
    extra = 0


@admin.register(ScoreRun)
class ScoreRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "period_start",
        "status",
        "is_active",
        "checksum",
        "completed_at",
    )
    list_filter = ("status", "is_active")
    inlines = [NetworkScoreInline]
