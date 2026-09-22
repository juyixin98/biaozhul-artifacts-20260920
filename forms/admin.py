from django.contrib import admin

from .models import Crew, FormTemplate, FormTemplateVersion, Project


class VersionInline(admin.TabularInline):
    model = FormTemplateVersion
    extra = 0
    fields = ("version", "min_supported_version", "published_by", "published_at")
    readonly_fields = ("version", "min_supported_version", "published_by", "published_at")
    can_delete = False
    max_num = 0


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(Crew)
class CrewAdmin(admin.ModelAdmin):
    list_display = ("name", "project")
    list_filter = ("project",)


@admin.register(FormTemplate)
class FormTemplateAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "project", "current_version")
    list_filter = ("project",)
    inlines = (VersionInline,)


@admin.register(FormTemplateVersion)
class FormTemplateVersionAdmin(admin.ModelAdmin):
    list_display = ("template", "version", "min_supported_version", "published_by", "published_at")
