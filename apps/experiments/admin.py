from django.contrib import admin

from .models import Experiment, ExperimentAssignment


class AssignmentInline(admin.TabularInline):
    model = ExperimentAssignment
    extra = 0


@admin.register(Experiment)
class ExperimentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "placement", "status", "created_by", "created_at")
    list_filter = ("status",)
    inlines = [AssignmentInline]
