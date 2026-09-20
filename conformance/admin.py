from django.contrib import admin

from .models import (
    AnalysisRun,
    Case,
    CaseAnalysis,
    Event,
    ImportBatch,
    ProcessTemplate,
    Project,
    ProjectMembership,
    TemplateVersion,
)

admin.site.register(Project)
admin.site.register(ProjectMembership)
admin.site.register(ProcessTemplate)
admin.site.register(TemplateVersion)
admin.site.register(Case)
admin.site.register(Event)
admin.site.register(ImportBatch)
admin.site.register(AnalysisRun)
admin.site.register(CaseAnalysis)
