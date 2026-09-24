// Package httpapi exposes the verifier over HTTP. Pure backend, no UI.
package httpapi

import (
	"encoding/json"
	"io"
	"net/http"

	"build-attestation/internal/attestation"
)

type Server struct {
	verifier *attestation.Verifier
	mux      *http.ServeMux
}

func New(v *attestation.Verifier) *Server {
	s := &Server{verifier: v, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /verify", s.handleVerify)
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

type verifyResponse struct {
	Accepted bool                `json:"accepted"`
	Reason   string              `json:"reason,omitempty"`
	Code     string              `json:"code,omitempty"`
	Result   *attestation.Result `json:"result,omitempty"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleVerify(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, verifyResponse{
			Accepted: false, Code: attestation.CodeMalformed,
			Reason: "request body too large or unreadable (limit 1 MiB)",
		})
		return
	}

	result, verr := s.verifier.Verify(body)
	if verr != nil {
		status := http.StatusForbidden
		switch verr.Code {
		case attestation.CodeMalformed, attestation.CodeNonCanonical,
			attestation.CodeDuplicateKey, attestation.CodeStatementInvalid:
			status = http.StatusBadRequest
		case attestation.CodeReplay:
			status = http.StatusConflict
		}
		writeJSON(w, status, verifyResponse{Accepted: false, Code: verr.Code, Reason: verr.Message})
		return
	}
	writeJSON(w, http.StatusOK, verifyResponse{Accepted: true, Result: result})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}
