// Package httpserver exposes the indexer API with chi.
package httpserver

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"forkindexer/internal/domain"
	"forkindexer/internal/pgstore"

	"github.com/go-chi/chi/v5"
)

// Server wires the store to HTTP handlers.
type Server struct {
	store *pgstore.Store
	r     chi.Router
}

// New constructs the router.
func New(store *pgstore.Store) *Server {
	s := &Server{store: store}
	r := chi.NewRouter()

	r.Get("/healthz", s.handleHealth)
	r.Get("/v1/chain/head", s.handleHead)

	r.Post("/v1/blocks", s.handlePostBlock)
	r.Post("/v1/envelopes", s.handlePostEnvelopes)

	r.Get("/v1/blocks/{hash}", s.handleGetBlock)
	r.Get("/v1/blocks/{hash}/transactions", s.handleBlockTransactions)
	r.Get("/v1/chain/blocks", s.handleChainBlocks)
	r.Get("/v1/addresses/{addr}/balance", s.handleBalance)
	r.Get("/v1/addresses/{addr}/transactions", s.handleAddressTransactions)
	r.Get("/v1/verify", s.handleVerify)

	s.r = r
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.r }

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if err := s.store.Ping(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, "db_unavailable", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleHead(w http.ResponseWriter, r *http.Request) {
	v, err := s.store.Head(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "query_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, v)
}

// postBlockRequest accepts either a bare block or one envelope-shaped body.
// We parse strictly by known fields.
func (s *Server) handlePostBlock(w http.ResponseWriter, r *http.Request) {
	var raw map[string]json.RawMessage
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&raw); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if blkBody, ok := raw["block"]; ok {
		var seq int64
		if sb, ok := raw["sequence"]; ok {
			if err := json.Unmarshal(sb, &seq); err != nil {
				writeError(w, http.StatusBadRequest, "invalid_sequence", err.Error())
				return
			}
		} else {
			writeError(w, http.StatusBadRequest, "invalid_envelope", "sequence required when block wrapper used")
			return
		}
		var b domain.Block
		if err := json.Unmarshal(blkBody, &b); err != nil {
			writeError(w, http.StatusBadRequest, "invalid_block", err.Error())
			return
		}
		if err := domain.Normalize(&b); err != nil {
			mapDomainError(w, err)
			return
		}
		res, err := s.store.IngestEnvelope(r.Context(), &b, &seq)
		if err != nil {
			mapStoreError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, res)
		return
	}
	var b domain.Block
	if err := decodeStrict(raw, &b); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_block", err.Error())
		return
	}
	if err := domain.Normalize(&b); err != nil {
		mapDomainError(w, err)
		return
	}
	res, err := s.store.IngestEnvelope(r.Context(), &b, nil)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handlePostEnvelopes(w http.ResponseWriter, r *http.Request) {
	body := http.MaxBytesReader(w, r.Body, 16<<20)
	var envelopes []domain.Envelope
	if err := json.NewDecoder(body).Decode(&envelopes); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "expected a JSON array of envelopes: "+err.Error())
		return
	}
	if len(envelopes) == 0 {
		writeError(w, http.StatusBadRequest, "empty_batch", "at least one envelope required")
		return
	}
	for i := range envelopes {
		if err := domain.Normalize(&envelopes[i].Block); err != nil {
			writeError(w, http.StatusUnprocessableEntity, "invalid_block",
				"envelope "+strconv.Itoa(i)+": "+err.Error())
			return
		}
		if envelopes[i].Sequence <= 0 {
			writeError(w, http.StatusBadRequest, "invalid_sequence",
				"envelope sequence must be >= 1")
			return
		}
	}
	results, err := s.store.IngestBatch(r.Context(), envelopes)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ingested": len(results), "results": results})
}

func (s *Server) handleGetBlock(w http.ResponseWriter, r *http.Request) {
	hash := chi.URLParam(r, "hash")
	v, err := s.store.Block(r.Context(), hash)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) handleBlockTransactions(w http.ResponseWriter, r *http.Request) {
	hash := chi.URLParam(r, "hash")
	txs, err := s.store.BlockTransactions(r.Context(), hash)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"block_hash": hash, "transactions": txs})
}

func (s *Server) handleChainBlocks(w http.ResponseWriter, r *http.Request) {
	start, _ := strconv.ParseInt(r.URL.Query().Get("start_height"), 10, 64)
	limit, _ := strconv.ParseInt(r.URL.Query().Get("limit"), 10, 64)
	blocks, err := s.store.ChainBlocks(r.Context(), start, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "query_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"blocks": blocks})
}

func (s *Server) handleBalance(w http.ResponseWriter, r *http.Request) {
	addr := chi.URLParam(r, "addr")
	bal, head, err := s.store.Balance(r.Context(), addr)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"address": addr, "balance": bal, "head": head,
	})
}

func (s *Server) handleAddressTransactions(w http.ResponseWriter, r *http.Request) {
	addr := chi.URLParam(r, "addr")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	txs, err := s.store.AddressTransactions(r.Context(), addr, limit)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"address": addr, "transactions": txs})
}

func (s *Server) handleVerify(w http.ResponseWriter, r *http.Request) {
	rep, err := s.store.VerifyFromGenesis(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "verify_failed", err.Error())
		return
	}
	status := http.StatusOK
	if !rep.OK {
		status = http.StatusConflict
	}
	writeJSON(w, status, rep)
}

// decodeStrict re-encodes a generic map into the target struct, rejecting
// unknown fields at the top level via Marshal/Unmarshal DisallowUnknownFields.
func decodeStrict(m map[string]json.RawMessage, dst any) error {
	raw, err := json.Marshal(m)
	if err != nil {
		return err
	}
	dec := json.NewDecoder(bytesReader(raw))
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}

func mapDomainError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, domain.ErrInvalidHash),
		errors.Is(err, domain.ErrInvalidAddress),
		errors.Is(err, domain.ErrHeightNegative),
		errors.Is(err, domain.ErrNegativeAmount):
		writeError(w, http.StatusUnprocessableEntity, "invalid_block", err.Error())
	case errors.Is(err, domain.ErrHashMismatch):
		writeError(w, http.StatusUnprocessableEntity, "hash_mismatch", err.Error())
	default:
		writeError(w, http.StatusUnprocessableEntity, "invalid_block", err.Error())
	}
}

func mapStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, pgstore.ErrNotFound):
		writeError(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, domain.ErrSameHashDifferentContent):
		writeError(w, http.StatusConflict, "same_hash_different_content", err.Error())
	case errors.Is(err, domain.ErrSequenceGap):
		writeError(w, http.StatusConflict, "sequence_gap", err.Error())
	case errors.Is(err, domain.ErrSequenceTooSmall):
		writeError(w, http.StatusConflict, "sequence_reuse", err.Error())
	case errors.Is(err, domain.ErrHeightParentMismatch),
		errors.Is(err, domain.ErrGenesisParent),
		errors.Is(err, domain.ErrNonGenesisParent),
		errors.Is(err, domain.ErrDuplicateGenesis),
		errors.Is(err, domain.ErrParentRejected),
		errors.Is(err, domain.ErrBalanceOverflow),
		errors.Is(err, domain.ErrAmountOverflow):
		writeError(w, http.StatusUnprocessableEntity, "rejected", err.Error())
	case errors.Is(err, domain.ErrInvalidHash),
		errors.Is(err, domain.ErrInvalidAddress):
		writeError(w, http.StatusBadRequest, "invalid_parameter", err.Error())
	default:
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error())
	}
}
