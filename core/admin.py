from django.contrib import admin

from .models import (
    ChangeLog,
    Conflict,
    FormRecord,
    FormTemplate,
    Profile,
    Project,
    RecordVersion,
    SyncBatch,
    SyncItem,
    SyncState,
    Team,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "created_at")
    search_fields = ("code", "name")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "created_at")
    filter_horizontal = ("projects", "supervisors", "members")
    search_fields = ("code", "name")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)
    filter_horizontal = ("assigned_projects",)


@admin.register(FormTemplate)
class FormTemplateAdmin(admin.ModelAdmin):
    list_display = ("code", "version", "project", "status", "published_at")
    list_filter = ("status", "project")
    search_fields = ("code",)


class RecordVersionInline(admin.TabularInline):
    model = RecordVersion
    extra = 0
    fields = ("id", "content_hash", "client_record_version", "status", "created_by",
              "collected_at", "created_at")
    readonly_fields = fields


@admin.register(FormRecord)
class FormRecordAdmin(admin.ModelAdmin):
    list_display = ("uuid", "project", "template", "status", "created_at")
    list_filter = ("status", "project")
    search_fields = ("uuid",)
    inlines = [RecordVersionInline]


@admin.register(Conflict)
class ConflictAdmin(admin.ModelAdmin):
    list_display = ("id", "record", "project", "status", "resolved_by", "resolved_at")
    list_filter = ("status", "project")
    filter_horizontal = ("versions",)


@admin.register(ChangeLog)
class ChangeLogAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "project", "record", "created_at")
    list_filter = ("kind", "project")
    search_fields = ("record__uuid",)


@admin.register(SyncBatch)
class SyncBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "project", "client_batch_id", "status", "item_count",
                    "created_at")
    list_filter = ("status", "project")
    search_fields = ("client_batch_id",)


@admin.register(SyncItem)
class SyncItemAdmin(admin.ModelAdmin):
    list_display = ("batch", "index", "uuid", "status", "code")
    list_filter = ("status",)


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ("user", "project", "cursor", "hwm", "updated_at")
