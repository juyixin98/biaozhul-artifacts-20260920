package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"

	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
)

// Server wires the service to HTTP routes.
type Server struct {
	svc *service.Service
}

func NewServer(svc *service.Service) http.Handler {
	s := &Server{svc: svc}
	r := chi.NewRouter()

	r.Use(recoverer)
	r.Get("/healthz", s.health)

	r.Route("/v1", func(r chi.Router) {
		r.Use(s.authenticate)

		// Merchant administration (admin only).
		r.With(requireRole("admin")).Post("/admin/merchants", s.createMerchant)
		r.With(requireRole("admin")).Get("/admin/merchants", s.listMerchants)
		r.With(requireRole("admin")).Patch("/admin/merchants/{merchantID}", s.updateMerchant)
		r.With(requireRole("admin")).Post("/admin/keys", s.issueKey)
		r.With(requireRole("admin")).Post("/admin/statements", s.importStatement)
		r.With(requireRole("admin")).Post("/admin/statements/sync", s.syncStatements)
		r.With(requireRole("admin")).Post("/admin/corrections", s.postCorrection)

		// Money-moving operations (operators + admins).
		r.With(requireRole("admin", "operator")).Post("/payments/authorize", s.authorize)
		r.With(requireRole("admin", "operator")).Post("/payments/capture", s.capture)
		r.With(requireRole("admin", "operator")).Post("/payments/void", s.void)
		r.With(requireRole("admin", "operator")).Post("/payments/refund", s.refund)
		r.With(requireRole("admin")).Post("/merchants/{merchantID}/settle", s.settle)
		r.With(requireRole("admin")).Post("/merchants/{merchantID}/reconcile", s.reconcile)

		// Reads — any authenticated role, scoped to the merchant.
		r.Get("/merchants", s.listMerchants)
		r.Get("/merchants/{merchantID}", s.getMerchant)
		r.Get("/merchants/{merchantID}/payments", s.listPayments)
		r.Get("/merchants/{merchantID}/refunds", s.listRefunds)
		r.Get("/merchants/{merchantID}/batches", s.listBatches)
		r.Get("/merchants/{merchantID}/reconcile", s.listReconRuns)
		r.Get("/merchants/{merchantID}/reconcile/{date}", s.getReconRun)
		r.Get("/merchants/{merchantID}/balances", s.balances)
		r.Get("/payments/{paymentID}", s.getPayment)
		r.Get("/batches/{batchID}", s.getBatch)
		r.Get("/ledger/{refType}/{refID}", s.getLedger)
		r.Get("/audit", s.listAudit)
		r.With(requireRole("admin")).Get("/admin/keys", s.listKeys)
	})

	return r
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ---- helpers --------------------------------------------------------------

func decode(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	status := http.StatusInternalServerError
	switch {
	case errors.Is(err, domain.ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, domain.ErrForbidden):
		status = http.StatusForbidden
	case errors.Is(err, domain.ErrIdempotencyConflict), errors.Is(err, domain.ErrConflict):
		status = http.StatusConflict
	case errors.Is(err, domain.ErrInvalidState),
		errors.Is(err, domain.ErrAuthExpired),
		errors.Is(err, domain.ErrRefundWindowClosed),
		errors.Is(err, domain.ErrRefundTooLarge),
		errors.Is(err, domain.ErrAmountInvalid),
		errors.Is(err, domain.ErrCaptureTooLarge),
		errors.Is(err, domain.ErrValidation),
		errors.Is(err, domain.ErrSettlementExists):
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, map[string]string{"error": err.Error()})
}
