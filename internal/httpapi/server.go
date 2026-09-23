// Package httpapi exposes the credential index over HTTP using chi.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"vci/internal/domain"
	"vci/internal/service"
)

type Server struct {
	svc *service.Service
}

func NewServer(svc *service.Service) *Server { return &Server{svc: svc} }

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RealIP)
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))

	r.Get("/healthz", s.health)
	r.Get("/v1/snapshot", s.headSnapshot)

	r.Route("/v1/issuers", func(r chi.Router) {
		r.Post("/", s.createIssuer)
		r.Get("/{issuerID}", s.getIssuer)
		r.Post("/{issuerID}/keys/rotate", s.rotateKey)
		r.Post("/{issuerID}/keys/retire", s.retireKey)
	})
	r.Route("/v1/credentials", func(r chi.Router) {
		r.Post("/", s.issue)
		r.Post("/issue", s.issue)
		r.Get("/{credentialID}", s.getCredential)
		r.Post("/{credentialID}/revoke", s.revoke)
	})
	r.Post("/v1/verify", s.verify)
	r.Post("/v1/revocations", s.revoke)

	return r
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	head, err := s.svc.Head(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "DB unavailable: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "snapshot": head})
}

func (s *Server) headSnapshot(w http.ResponseWriter, r *http.Request) {
	head, err := s.svc.Head(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"snapshot": head})
}

type createIssuerReq struct {
	Name string `json:"name"`
}

func (s *Server) createIssuer(w http.ResponseWriter, r *http.Request) {
	var req createIssuerReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	res, err := s.svc.CreateIssuer(r.Context(), req.Name)
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) getIssuer(w http.ResponseWriter, r *http.Request) {
	iss, err := s.svc.GetIssuerHTTP(r.Context(), chi.URLParam(r, "issuerID"))
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, iss)
}

type rotateReq struct {
	ClosePrevious *bool `json:"close_previous"` // default true
}

func (s *Server) rotateKey(w http.ResponseWriter, r *http.Request) {
	var req rotateReq
	closeOld := true
	if r.ContentLength != 0 {
		if err := decode(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		if req.ClosePrevious != nil {
			closeOld = *req.ClosePrevious
		}
	}
	res, err := s.svc.RotateKey(r.Context(), chi.URLParam(r, "issuerID"), closeOld)
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) retireKey(w http.ResponseWriter, r *http.Request) {
	res, err := s.svc.RetireKey(r.Context(), chi.URLParam(r, "issuerID"))
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

type issueReq struct {
	IssuerID  string          `json:"issuer_id"`
	Subject   string          `json:"subject"`
	Purpose   string          `json:"purpose"`
	NotBefore *time.Time      `json:"not_before,omitempty"`
	ExpiresAt time.Time       `json:"expires_at"`
	Content   json.RawMessage `json:"content"`
}

func (s *Server) issue(w http.ResponseWriter, r *http.Request) {
	var req issueReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.IssuerID == "" {
		req.IssuerID = chi.URLParam(r, "issuerID")
	}
	res, err := s.svc.IssueCredential(r.Context(), service.IssueRequest{
		IssuerID: req.IssuerID, Subject: req.Subject, Purpose: req.Purpose,
		NotBefore: req.NotBefore, ExpiresAt: req.ExpiresAt, Content: req.Content,
	})
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) getCredential(w http.ResponseWriter, r *http.Request) {
	res, err := s.svc.GetCredentialHTTP(r.Context(), chi.URLParam(r, "credentialID"))
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

type revokeReq struct {
	CredentialID string     `json:"credential_id"`
	Reason       string     `json:"reason"`
	EffectiveAt  *time.Time `json:"effective_at,omitempty"`
}

func (s *Server) revoke(w http.ResponseWriter, r *http.Request) {
	var req revokeReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.CredentialID == "" {
		req.CredentialID = chi.URLParam(r, "credentialID")
	}
	res, err := s.svc.Revoke(r.Context(), service.RevokeInput{
		CredentialID: req.CredentialID, Reason: req.Reason, EffectiveAt: req.EffectiveAt,
	})
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

type verifyReq struct {
	CredentialID    string          `json:"credential_id"`
	ExpectedPurpose string          `json:"expected_purpose"`
	AsOf            *time.Time      `json:"as_of,omitempty"`
	Content         json.RawMessage `json:"content,omitempty"`
}

func (s *Server) verify(w http.ResponseWriter, r *http.Request) {
	var req verifyReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	res, err := s.svc.VerifyHTTP(r.Context(), service.VerifyInput{
		CredentialID:    req.CredentialID,
		ExpectedPurpose: req.ExpectedPurpose,
		AsOf:            req.AsOf,
		Content:         req.Content,
	})
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// ---------------- helpers ----------------

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

type errBody struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, errBody{Error: msg, Code: httpCode(status)})
}

func httpCode(status int) string {
	switch status {
	case http.StatusBadRequest:
		return "bad_request"
	case http.StatusNotFound:
		return "not_found"
	case http.StatusConflict:
		return "conflict"
	case http.StatusUnprocessableEntity:
		return "unprocessable"
	case http.StatusServiceUnavailable:
		return "unavailable"
	default:
		return "error"
	}
}

func writeDomainError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, domain.ErrNotFound), errors.Is(err, domain.ErrIssuerNotFound):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, domain.ErrConflict):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, domain.ErrNoActiveKey):
		writeError(w, http.StatusUnprocessableEntity, err.Error())
	default:
		writeError(w, http.StatusInternalServerError, err.Error())
	}
}
