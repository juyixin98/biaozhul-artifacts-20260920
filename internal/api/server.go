// Package api exposes the indexer over HTTP using chi.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"forkindexer/internal/indexer"
	"forkindexer/internal/model"

	"github.com/go-chi/chi/v5"
)

type Server struct {
	ix *indexer.Indexer
}

func NewServer(ix *indexer.Indexer) *Server { return &Server{ix: ix} }

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/v1", func(r chi.Router) {
		r.Post("/blocks", s.postBlocks)
		r.Get("/head", s.getHead)
		r.Get("/state", s.getState)
		r.Get("/blocks/{hash}", s.getBlock)
		r.Get("/height/{height}", s.getAtHeight)
		r.Get("/chain", s.getChain)
		r.Get("/accounts/{address}/balance", s.getBalance)
		r.Get("/accounts/{address}/transactions", s.getAccountTransactions)
		r.Post("/verify/rebuild", s.postVerifyRebuild)
	})
	return r
}

// postBlocks accepts either a single JSON block or newline-delimited JSON
// blocks (application/x-ndjson). An optional ?offset=N records the durable
// stream cursor; it commits atomically with the blocks and balances.
func (s *Server) postBlocks(w http.ResponseWriter, r *http.Request) {
	var offset *int64
	if raw := r.URL.Query().Get("offset"); raw != "" {
		v, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || v < 0 {
			writeError(w, http.StatusBadRequest, "offset must be a non-negative integer")
			return
		}
		offset = &v
	}

	blocks, err := decodeBlocks(w, r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(blocks) == 0 {
		writeError(w, http.StatusBadRequest, "no blocks in request body")
		return
	}

	res, err := s.ix.Ingest(r.Context(), blocks, offset)
	if err != nil {
		var bad *indexer.BadBlockError
		if errors.As(err, &bad) {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		var conflict *indexer.HashConflictError
		if errors.As(err, &conflict) {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// decodeBlocks supports both {"hash":...} single-object bodies and
// NDJSON (one block object per non-empty line). A JSON array is also accepted.
func decodeBlocks(w http.ResponseWriter, r *http.Request) ([]model.Block, error) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 64<<20))
	if err != nil {
		return nil, err
	}
	trimmed := strings.TrimSpace(string(body))
	if trimmed == "" {
		return nil, errors.New("empty body")
	}

	if strings.HasPrefix(trimmed, "[") {
		var arr []model.Block
		if err := json.Unmarshal(body, &arr); err != nil {
			return nil, errors.New("invalid JSON array of blocks: " + err.Error())
		}
		return arr, nil
	}

	// Single object?
	first := trimmed[0]
	if first == '{' && !strings.ContainsAny(trimmed, "\n\r") {
		var b model.Block
		if err := json.Unmarshal(body, &b); err != nil {
			return nil, errors.New("invalid block JSON: " + err.Error())
		}
		return []model.Block{b}, nil
	}

	// NDJSON stream.
	var out []model.Block
	dec := json.NewDecoder(strings.NewReader(trimmed))
	for dec.More() {
		var b model.Block
		if err := dec.Decode(&b); err != nil {
			return nil, errors.New("invalid NDJSON block: " + err.Error())
		}
		out = append(out, b)
	}
	return out, nil
}

func (s *Server) getHead(w http.ResponseWriter, r *http.Request) {
	h, err := s.ix.Head(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if h == nil {
		writeJSON(w, http.StatusOK, map[string]any{"canonical": false})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"canonical": true, "hash": h.Hash, "height": h.Height})
}

func (s *Server) getState(w http.ResponseWriter, r *http.Request) {
	st, err := s.ix.State(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) getBlock(w http.ResponseWriter, r *http.Request) {
	b, err := s.ix.GetBlock(r.Context(), strings.ToLower(chi.URLParam(r, "hash")))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if b == nil {
		writeError(w, http.StatusNotFound, "block not found")
		return
	}
	writeJSON(w, http.StatusOK, b)
}

func (s *Server) getAtHeight(w http.ResponseWriter, r *http.Request) {
	h, err := strconv.ParseInt(chi.URLParam(r, "height"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "height must be an integer")
		return
	}
	b, err := s.ix.BlockAtHeight(r.Context(), h)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if b == nil {
		writeError(w, http.StatusNotFound, "no canonical block at that height")
		return
	}
	writeJSON(w, http.StatusOK, b)
}

func (s *Server) getChain(w http.ResponseWriter, r *http.Request) {
	from := strings.ToLower(r.URL.Query().Get("from"))
	blocks, err := s.ix.Chain(r.Context(), from)
	if err != nil {
		if errors.Is(err, indexer.ErrNotCanonical) {
			writeError(w, http.StatusNotFound, "from hash is not on the canonical chain")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"blocks": blocks, "depth": len(blocks)})
}

func (s *Server) getBalance(w http.ResponseWriter, r *http.Request) {
	addr := strings.ToLower(chi.URLParam(r, "address"))
	bal, err := s.ix.Balance(r.Context(), addr)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"address": addr, "balance": bal})
}

func (s *Server) getAccountTransactions(w http.ResponseWriter, r *http.Request) {
	addr := strings.ToLower(chi.URLParam(r, "address"))
	limit := 100
	if raw := r.URL.Query().Get("limit"); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, "limit must be an integer")
			return
		}
		limit = v
	}
	rows, err := s.ix.AccountTransactions(r.Context(), addr, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"transactions": rows})
}

func (s *Server) postVerifyRebuild(w http.ResponseWriter, r *http.Request) {
	rep, err := s.ix.VerifyAgainstRebuild(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	code := http.StatusOK
	if !rep.Match {
		code = http.StatusExpectationFailed
	}
	writeJSON(w, code, rep)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
