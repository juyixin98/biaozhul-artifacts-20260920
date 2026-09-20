package service

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"github.com/jackc/pgx/v5/pgconn"

	"sircc/internal/db"
	"sircc/internal/httpx"
)

// result captures what a mutating handler wants to send back.
type result struct {
	status int
	body   any
}

// idemExec runs fn inside one transaction that also owns the
// idempotent_requests reservation, so the reservation and the business write
// commit or roll back together.
//
// Semantics:
//   - First call with a request id: reserve -> execute -> store response.
//   - Replay with the same request id: return the stored response/status
//     verbatim, no business logic runs a second time.
//   - A reservation whose row is still status_code=0 (a crashed in-flight
//     request) is treated as 409 duplicate_request: the client must retry
//     with a fresh id rather than silently executing twice.
//   - Missing request id on a mutating call: 400.
func (s *Service) idemExec(
	w http.ResponseWriter,
	r *http.Request,
	requestID string,
	fn func(ctx context.Context, q db.Querier) result,
) {
	if requestID == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "missing X-Request-Id header (idempotency key required)")
		return
	}
	actor, _ := actorFrom(r.Context())

	// Fast path: request id already known.
	existing, err := s.q.GetIdempotentRequest(r.Context(), requestID)
	if err == nil {
		if existing.StatusCode == 0 {
			// A prior attempt reserved the id but never committed a response
			// (crash mid-flight). Refuse rather than execute twice.
			httpx.ErrorJSON(w, http.StatusConflict, httpx.CodeDuplicate,
				"request id is reserved by an unfinished attempt; use a fresh X-Request-Id")
			return
		}
		replay(w, int(existing.StatusCode), existing.Response)
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "begin tx")
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()

	qtx := s.q.WithTx(tx)

	inserted, err := qtx.InsertIdempotentRequest(r.Context(), db.InsertIdempotentRequestParams{
		RequestID: requestID,
		ActorID:   actor.ID,
		Method:    r.Method,
		Path:      r.URL.Path,
	})
	if err != nil {
		if isUniqueViolation(err) {
			// Lost the race to another in-flight request; read committed state.
			row, getErr := s.q.GetIdempotentRequest(r.Context(), requestID)
			if getErr == nil {
				if row.StatusCode == 0 {
					httpx.ErrorJSON(w, http.StatusConflict, httpx.CodeDuplicate, "request is already being processed")
					return
				}
				replay(w, int(row.StatusCode), row.Response)
				return
			}
		}
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "idempotency reserve")
		return
	}
	_ = inserted

	res := fn(r.Context(), qtx)

	bodyBytes, err := json.Marshal(res.body)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "encode response")
		return
	}
	if err := qtx.CompleteIdempotentRequest(r.Context(), db.CompleteIdempotentRequestParams{
		RequestID:  requestID,
		Response:   bodyBytes,
		StatusCode: int32(res.status),
	}); err != nil {
		log.Printf("store idempotent response: %v", err)
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "store response")
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "commit")
		return
	}

	// Errors returned inside fn are already encoded into a normal JSON body
	// (e.g. 409) so they are cached as the canonical response.
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(res.status)
	_, _ = w.Write(bodyBytes)
}

func replay(w http.ResponseWriter, status int, payload []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Idempotent-Replay", "true")
	w.WriteHeader(int(status))
	_, _ = w.Write(payload)
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "23505"
	}
	return false
}
