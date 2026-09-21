from django.contrib import admin

from .models import (
    AdNetwork,
    App,
    AuditLog,
    ConfigVersion,
    ConfigVersionEntry,
    Placement,
    PlacementNetwork,
)


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "owner", "created_at")
    list_filter = ("owner",)
    readonly_fields = ("sdk_key",)


@admin.register(Placement)
class PlacementAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "app", "format", "active_version")
    list_filter = ("format", "app__owner")


@admin.register(PlacementNetwork)
class PlacementNetworkAdmin(admin.ModelAdmin):
    list_display = ("id", "placement", "network", "priority", "cpm_floor", "fallback_order", "enabled")


class ConfigVersionEntryInline(admin.TabularInline):
    model = ConfigVersionEntry
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ConfigVersion)
class ConfigVersionAdmin(admin.ModelAdmin):
    list_display = ("id", "placement", "version", "status", "published_by", "published_at")
    inlines = [ConfigVersionEntryInline]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "actor", "action", "app", "target_repr")
    list_filter = ("action", "app")


admin.site.register(AdNetwork)
