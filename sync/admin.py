from django.contrib import admin

from .models import (
    FormRecord,
    RecordChange,
    RecordRevision,
    SyncBatch,
    SyncResultEntry,
)


class RevisionInline(admin.TabularInline):
    model = RecordRevision
    extra = 0
    fields = (
        "seq",
        "kind",
        "base_version",
        "template_version",
        "submitted_by",
        "collected_at",
        "content_hash",
    )
    readonly_fields = fields
    can_delete = False
    max_num = 0


@admin.register(FormRecord)
class FormRecordAdmin(admin.ModelAdmin):
    list_display = ("record_uuid", "template", "crew", "status", "current_revision")
    list_filter = ("status", "project", "crew")
    search_fields = ("record_uuid",)
    inlines = (RevisionInline,)


@admin.register(RecordRevision)
class RecordRevisionAdmin(admin.ModelAdmin):
    list_display = ("record", "seq", "kind", "template_version", "submitted_by")
    list_filter = ("kind",)


@admin.register(RecordChange)
class RecordChangeAdmin(admin.ModelAdmin):
    list_display = ("id", "record", "change_type", "record_status", "created_at")
    list_filter = ("change_type", "record_status")


class ResultInline(admin.TabularInline):
    model = SyncResultEntry
    extra = 0
    readonly_fields = (
        "client_uuid",
        "status",
        "status_code",
        "record_version",
        "revision_seq",
        "content_hash",
        "errors",
        "conflict_with_seq",
    )
    can_delete = False
    max_num = 0


@admin.register(SyncBatch)
class SyncBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "submitted_by", "client_batch_id", "entry_count", "created_at")
    inlines = (ResultInline,)
