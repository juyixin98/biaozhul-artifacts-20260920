package api

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"inbox/internal/core"
)

// Server exposes the inbox over HTTP.
type Server struct {
	ex *core.Executor
}

// NewRouter builds the Chi router.
func NewRouter(ex *core.Executor) http.Handler {
	s := &Server{ex: ex}
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Recoverer)
	r.Use(jsonContentType)

	r.Get("/healthz", s.health)

	r.Route("/v1", func(r chi.Router) {
		r.Post("/chains/{chainID}/channels", s.openChannel)
		r.Get("/chains/{chainID}/channels/{channelID}", s.getChannel)

		r.Post("/headers", s.submitHeader)
		r.Post("/revocations", s.submitRevocation)
		r.Post("/messages", s.submitMessage)

		r.Post("/process", s.process)

		r.Get("/chains/{chainID}/headers", s.listHeaders)
		r.Get("/chains/{chainID}/channels/{channelID}/messages", s.listMessages)
		r.Get("/chains/{chainID}/channels/{channelID}/deliveries", s.listDeliveries)
		r.Get("/alerts", s.listAlerts)
	})
	return r
}

func jsonContentType(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		next.ServeHTTP(w, r)
	})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func decode(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code core.ErrorCode, msg string) {
	writeJSON(w, status, map[string]string{"error": msg, "code": string(code)})
}

func failDomain(w http.ResponseWriter, err error) {
	var de *core.DomainError
	if errors.As(err, &de) {
		writeErr(w, statusForCode(de.Code), de.Code, de.Msg)
		return
	}
	writeErr(w, http.StatusInternalServerError, core.CodeInternal, err.Error())
}

// statusForCode maps domain error codes to HTTP statuses.
func statusForCode(c core.ErrorCode) int {
	switch c {
	case core.CodeBadRequest:
		return http.StatusBadRequest
	case core.CodeUnknownChain, core.CodeUnknownChannel, core.CodeBlockUnknown:
		return http.StatusNotFound
	case core.CodeBadSignature, core.CodeBadProof:
		return http.StatusUnauthorized
	case core.CodeChannelFrozen:
		return http.StatusLocked
	case core.CodeBadParent, core.CodeHeaderGap, core.CodeBlockNotCanonical,
		core.CodeTipNotCurrent, core.CodeBlockFinalized:
		return http.StatusUnprocessableEntity
	case core.CodeRevokeFirst, core.CodeEquivocation, core.CodeConflictFrozen:
		return http.StatusConflict
	case core.CodeConflict:
		return http.StatusConflict
	case core.CodeDuplicateHeader, core.CodeAlreadyExists:
		return http.StatusConflict
	default:
		return http.StatusInternalServerError
	}
}

func mustHex(s string, n int) ([]byte, error) {
	b, err := hex.DecodeString(s)
	if err != nil {
		return nil, errors.New("invalid hex encoding")
	}
	if n >= 0 && len(b) != n {
		return nil, errors.New("unexpected byte length")
	}
	return b, nil
}
