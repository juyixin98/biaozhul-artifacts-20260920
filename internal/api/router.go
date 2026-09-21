package api

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"github.com/clearsettle/clearsettle/internal/auth"
)

// NewRouter builds the complete HTTP mux.
func NewRouter(d Deps) http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RealIP)
	r.Use(requestID)
	r.Use(middleware.Recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, req *http.Request) {
		if err := d.Pool.Ping(req.Context()); err != nil {
			writeError(w, http.StatusServiceUnavailable, "db_unavailable", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/v1", func(r chi.Router) {
		// Public auth endpoint.
		r.Post("/auth/login", d.handleLogin)

		// Merchant-facing money movement: JWT operator OR operator API key.
		r.Group(func(r chi.Router) {
			r.Use(d.authenticateWrite)
			r.Use(requireRole(auth.RoleOperator))
			r.Use(d.merchantScope)

			r.Post("/payments/authorize", d.handleAuthorize)
			r.Post("/payments/{id}/capture", d.handleCapture)
			r.Post("/payments/{id}/void", d.handleVoid)
			r.Post("/payments/{id}/refund", d.handleRefund)
		})

		// Read-only views: operator (own merchant), auditor (all merchants),
		// or an operator API key (own merchant).
		r.Group(func(r chi.Router) {
			r.Use(d.authenticateRead)

			r.Get("/payments", d.handleListPayments)
			r.Get("/payments/{id}", d.handleGetPayment)
			r.Get("/payments/{id}/refunds", d.handleListRefunds)
			r.Get("/accounts/balance", d.handleBalance)
			r.Get("/settlements", d.handleListSettlements)
			r.Get("/settlements/{id}", d.handleGetSettlement)
			r.Get("/reconciliations", d.handleListRecon)
			r.Get("/reconciliations/{date}", d.handleGetRecon)
		})

		// Operator/auditor may trigger jobs manually (auditor cannot).
		r.Group(func(r chi.Router) {
			r.Use(d.authenticateUser)
			r.Use(requireRole(auth.RoleOperator))
			r.Post("/jobs/settle", d.handleSettleNow)
			r.Post("/jobs/reconcile/{date}", d.handleReconcileNow)
		})

		// Admin-only user/merchant management.
		r.Group(func(r chi.Router) {
			r.Use(d.authenticateUser)
			r.Use(requireRole(auth.RoleAdmin))

			r.Post("/admin/merchants", d.handleCreateMerchant)
			r.Get("/admin/merchants", d.handleListMerchants)
			r.Post("/admin/merchants/{id}/rotate-key", d.handleRotateKey)
			r.Post("/admin/merchants/{id}/status", d.handleMerchantStatus)
			r.Post("/admin/users", d.handleCreateUser)
			r.Get("/admin/users", d.handleListUsers)
		})

		// Audit log: admin sees all; auditor sees all (read-only role).
		r.Group(func(r chi.Router) {
			r.Use(d.authenticateUser)
			r.Use(requireAnyRole(auth.RoleAdmin, auth.RoleAuditor))
			r.Get("/audit-logs", d.handleListAudit)
		})
	})

	return r
}
