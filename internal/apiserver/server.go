// Package apiserver wires Chi routes, API-key authentication and handlers.
package apiserver

import (
	"encoding/json"
	"errors"
	"net/http"
	"reflect"

	"costlens/internal/auth"
	"costlens/internal/service"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Server struct {
	pool *pgxpool.Pool
	svc  *service.Service
}

func New(pool *pgxpool.Pool) http.Handler {
	s := &Server{pool: pool, svc: service.New(pool)}
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/v1", func(r chi.Router) {
		r.Use(s.authenticate)

		r.Get("/organizations", s.listOrganizations)
		r.Get("/cost-centers", s.listCostCenters)
		r.Get("/accounts", s.listAccounts)

		r.Post("/organizations/{org_id}/imports", s.createImport)
		r.Get("/imports", s.listImports)

		r.Get("/costs", s.listCosts)
		r.Get("/export/costs.csv", s.exportCosts)

		r.Get("/summaries/daily-account", s.dailyAccount)
		r.Get("/summaries/monthly-account", s.monthlyAccount)
		r.Get("/summaries/daily-cost-center", s.dailyCostCenter)
		r.Get("/summaries/monthly-cost-center", s.monthlyCostCenter)

		r.Put("/budgets", s.putBudget)
		r.Get("/budgets", s.listBudgets)
		r.Get("/budget-alerts", s.listBudgetAlerts)

		r.Get("/anomalies", s.listAnomalies)
		r.Get("/anomalies/history", s.listAnomalyHistory)

		r.Post("/admin/rebuild", s.rebuild)
	})

	return r
}

func (s *Server) authenticate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, err := auth.Authenticate(r.Context(), s.pool, r.Header.Get("Authorization"))
		if err != nil {
			writeError(w, http.StatusUnauthorized, "unauthorized", err.Error())
			return
		}
		next.ServeHTTP(w, r.WithContext(auth.WithPrincipal(r.Context(), p)))
	})
}

// ---- helpers ----

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

// list normalizes nil slices to [] so empty result sets serialize as [], not
// null.
func list(items any) any {
	if items == nil {
		return []any{}
	}
	v := reflect.ValueOf(items)
	if v.Kind() == reflect.Slice && v.IsNil() {
		return reflect.MakeSlice(v.Type(), 0, 0).Interface()
	}
	return items
}

func writeServiceError(w http.ResponseWriter, err error) {
	var ve *service.ValidationError
	if errors.As(err, &ve) {
		status := http.StatusUnprocessableEntity
		if ve.Code == "forbidden" || ve.Code == "forbidden_org" {
			status = http.StatusForbidden
		}
		if ve.Code == "cost_center_not_found" {
			status = http.StatusNotFound
		}
		writeJSON(w, status, map[string]any{
			"error": ve.Code, "message": ve.Message, "line": ve.Line,
		})
		return
	}
	var ce *service.ConflictError
	if errors.As(err, &ce) {
		writeJSON(w, http.StatusConflict, map[string]any{
			"error": "content_conflict", "message": ce.Error(), "line": ce.Line, "key": ce.Key,
		})
		return
	}
	if errors.Is(err, service.ErrNotFound) || errors.Is(err, pgx.ErrNoRows) {
		writeError(w, http.StatusNotFound, "not_found", "resource not found")
		return
	}
	writeError(w, http.StatusInternalServerError, "internal", err.Error())
}
