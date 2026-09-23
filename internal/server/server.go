// Package server wires the inbox service to an HTTP API using chi.
package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"ximbox/internal/cryptoenvelope"
	"ximbox/internal/inbox"
	"ximbox/internal/store"
)

// Server holds dependencies for the HTTP layer.
type Server struct {
	svc *inbox.Service
	st  *store.Store
}

// New builds the chi router.
func New(svc *inbox.Service, st *store.Store) http.Handler {
	s := &Server{svc: svc, st: st}
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/v1", func(r chi.Router) {
		r.Post("/blocks", s.proposeBlock)
		r.Post("/blocks/confirm", s.confirmBlock)
		r.Get("/blocks", s.listBlocks)

		r.Post("/messages", s.ingestMessage)
		r.Get("/messages", s.listMessages)
		r.Post("/channels/advance", s.advanceChannel)
		r.Get("/channels/{chain}/{channel}", s.getChannel)

		r.Get("/alerts", s.listAlerts)
		r.Get("/evidence", s.listEvidence)
		r.Get("/accounts", s.listAccounts)
	})
	return r
}

// handlers -----------------------------------------------------------------

type proposeBlockReq struct {
	SourceChain string `json:"source_chain"`
	Height      int64  `json:"height"`
	Hash        string `json:"hash"`
	ParentHash  string `json:"parent_hash"`
}

func (s *Server) proposeBlock(w http.ResponseWriter, r *http.Request) {
	var req proposeBlockReq
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	err := s.svc.ProposeBlock(r.Context(), req.SourceChain, req.Hash, req.ParentHash, req.Height)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"source_chain": req.SourceChain,
		"height":       req.Height,
		"hash":         req.Hash,
		"status":       "proposed",
	})
}

type confirmBlockReq struct {
	SourceChain string `json:"source_chain"`
	Hash        string `json:"hash"`
}

func (s *Server) confirmBlock(w http.ResponseWriter, r *http.Request) {
	var req confirmBlockReq
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	block, err := s.svc.ConfirmBlock(r.Context(), req.SourceChain, req.Hash)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, block)
}

func (s *Server) listBlocks(w http.ResponseWriter, r *http.Request) {
	chain := r.URL.Query().Get("source_chain")
	if chain == "" {
		writeError(w, http.StatusBadRequest, "source_chain query param is required")
		return
	}
	limit := queryLimit(r, 100)
	blocks, err := s.st.ListBlocks(r.Context(), chain, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"blocks": blocks})
}

func (s *Server) ingestMessage(w http.ResponseWriter, r *http.Request) {
	var env cryptoenvelope.Envelope
	if err := decodeJSON(r, &env); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	msg, froze, err := s.svc.IngestMessage(r.Context(), &env)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	status := http.StatusCreated
	resp := map[string]any{"message": msg, "channel_frozen": froze}
	writeJSON(w, status, resp)
}

func (s *Server) listMessages(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	chain, channel := q.Get("source_chain"), q.Get("channel")
	if chain == "" || channel == "" {
		writeError(w, http.StatusBadRequest, "source_chain and channel query params are required")
		return
	}
	msgs, err := s.st.ListMessages(r.Context(), chain, channel, queryLimit(r, 200))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"messages": msgs})
}

type channelReq struct {
	SourceChain string `json:"source_chain"`
	Channel     string `json:"channel"`
}

func (s *Server) advanceChannel(w http.ResponseWriter, r *http.Request) {
	var req channelReq
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.svc.AdvanceChannel(r.Context(), req.SourceChain, req.Channel); err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "advanced"})
}

func (s *Server) getChannel(w http.ResponseWriter, r *http.Request) {
	chain := chi.URLParam(r, "chain")
	channel := chi.URLParam(r, "channel")
	ch, err := s.st.GetChannel(r.Context(), chain, channel)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, "channel not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, ch)
}

func (s *Server) listAlerts(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	alerts, err := s.st.ListAlerts(r.Context(), q.Get("source_chain"), q.Get("channel"), queryLimit(r, 200))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"alerts": alerts})
}

func (s *Server) listEvidence(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	ev, err := s.st.ListEvidence(r.Context(), q.Get("source_chain"), q.Get("channel"), queryLimit(r, 200))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"evidence": ev})
}

func (s *Server) listAccounts(w http.ResponseWriter, r *http.Request) {
	accounts, err := s.st.ListAccounts(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": accounts})
}

// helpers ------------------------------------------------------------------

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return errors.New("invalid JSON body: " + err.Error())
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]any{"error": http.StatusText(status), "detail": detail})
}

func writeServiceError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, inbox.ErrFrozen):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, inbox.ErrFinalConflict):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, inbox.ErrBlockRevoked):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, inbox.ErrBlockMissing):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, inbox.ErrInvalidSignature):
		writeError(w, http.StatusUnauthorized, err.Error())
	case errors.Is(err, inbox.ErrBadRequest),
		errors.Is(err, store.ErrBadParent),
		errors.Is(err, store.ErrBadParentHeight):
		writeError(w, http.StatusBadRequest, err.Error())
	default:
		writeError(w, http.StatusInternalServerError, err.Error())
	}
}

func queryLimit(r *http.Request, def int) int {
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 || n > 1000 {
		return def
	}
	return n
}
