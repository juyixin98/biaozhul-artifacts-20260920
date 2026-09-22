// Package api wires HTTP routes to the service layer and enforces
// organization-scoped visibility for every read.
package api

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"

	"costlens/internal/auth"
	"costlens/internal/service"
)

type Handler struct {
	svc *service.Service
}

func NewRouter(svc *service.Service) http.Handler {
	h := &Handler{svc: svc}
	r := chi.NewRouter()
	r.Use(chimw.RequestID)
	r.Use(chimw.RealIP)
	r.Use(chimw.Recoverer)
	r.Use(chimw.Timeout(120 * time.Second))

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})

	r.Route("/api/v1", func(r chi.Router) {
		r.Use(auth.Middleware(svc.Pool()))

		// Catalog (read, scoped)
		r.Get("/organizations", h.listOrgs)
		r.Get("/cost-centers", h.listCostCenters)
		r.Get("/accounts", h.listAccounts)

		// Import & maintenance
		r.Post("/accounts/{externalID}/imports", h.importCSV)
		r.Get("/batches", h.listBatches)
		r.Post("/admin/rebuild", auth.RequireAdmin(h.rebuild))

		// Read models
		r.Get("/records", h.listRecords)
		r.Get("/exports/records.csv", h.exportRecords)
		r.Get("/summaries/daily", h.listDaily)
		r.Get("/summaries/monthly", h.listMonthly)
		r.Get("/budgets", h.listBudgets)
		r.Get("/budget-alerts", h.listBudgetAlerts)
		r.Get("/anomalies", h.listAnomalies)
		r.Get("/anomalies/versions", h.listAnomalyVersions)

		// Admin catalog management
		r.Post("/admin/organizations", auth.RequireAdmin(h.createOrg))
		r.Post("/admin/cost-centers", auth.RequireAdmin(h.createCostCenter))
		r.Post("/admin/accounts", auth.RequireAdmin(h.createAccount))
		r.Post("/admin/users", auth.RequireAdmin(h.createUser))
		r.Post("/admin/users/{username}/grants", auth.RequireAdmin(h.grant))
		r.Post("/admin/budgets", auth.RequireAdmin(h.setBudget))
	})

	return r
}
