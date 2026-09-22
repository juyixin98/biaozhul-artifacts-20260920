from django.contrib import admin

from .models import (
    Allocation,
    Job,
    Node,
    PreemptionRequest,
    PreemptionVictim,
    ResourcePool,
    SchedulingDecision,
)

admin.site.register(ResourcePool)
admin.site.register(Node)
admin.site.register(Job)
admin.site.register(Allocation)
admin.site.register(PreemptionRequest)
admin.site.register(PreemptionVictim)
admin.site.register(SchedulingDecision)
