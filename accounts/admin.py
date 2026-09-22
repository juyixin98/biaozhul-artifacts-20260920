from django.contrib import admin

from .models import CrewMembership, Profile, ProjectAssignment


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "display_name")
    list_filter = ("role",)


admin.site.register(ProjectAssignment)
admin.site.register(CrewMembership)
