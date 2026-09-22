"""Top-level URL routing for the RevStream API.

API surface (versioned under /api/v1):

  Management (Developer token auth, tenant-scoped):
    POST   /api/v1/auth/register/             self-serve signup -> token
    POST   /api/v1/auth/api-token/            obtain token for username/password
    CRUD   /api/v1/apps/                      developer's applications
    CRUD   /api/v1/networks/                  ad networks (max 8/placement enforced upstream)
    CRUD   /api/v1/placements/                ad placements
    GET    /api/v1/placements/{id}/versions/ list published versions
    POST   /api/v1/placements/{id}/draft/     create/replace editable draft
    POST   /api/v1/placements/{id}/publish/   freeze draft -> immutable version
    CRUD   /api/v1/experiments/               50/50 A/B config experiments
    POST   /api/v1/experiments/{id}/start/
    POST   /api/v1/experiments/{id}/stop/
    GET    /api/v1/experiments/{id}/stats/    per-variant fill rate / eCPM
    GET    /api/v1/scores/                    latest network scores
    GET    /api/v1/audit-logs/                configuration change audit trail

  SDK / ingestion (App API-key auth):
    GET    /api/v1/sdk/config/?placement_key=...&user_key=...&app_key=...
    POST   /api/v1/events/batch/              idempotent event batches (<= 2000)
"""
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("config.api_urls")),
]
