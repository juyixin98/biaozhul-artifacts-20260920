package service

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/domain"
	"sircc/internal/store"
)

// Result is what a service operation hands back to the HTTP layer. When
// Replay is non-nil it is the persisted body of an earlier, duplicated
// request and must be returned verbatim with Status.
type Result struct {
	Status int
	Body   any
	Replay json.RawMessage
}

// mutater performs the business operation inside the (possibly idempotent)
// transaction.
type mutater func(ctx context.Context, q *store.Queries) (body any, status int, err error)

// runMutated executes fn in a transaction. When requestID is not the zero
// UUID the whole call is idempotent: a repeated request id replays the
// stored original response, while a key reused with a different method or
// path (or by a different user) is rejected. Advisory xact locks serialize
// concurrent calls carrying the same request id so the original response is
// inserted exactly once.
func (s *Service) runMutated(ctx context.Context, userID uuid.UUID, requestID uuid.UUID,
	incidentID *uuid.UUID, method, path string, fn mutater,
) (Result, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Result{}, err
	}
	defer tx.Rollback(ctx)
	q := store.New(tx)

	if requestID != uuid.Nil {
		if err := q.AdvisoryXactLock(ctx, requestID.String()); err != nil {
			return Result{}, err
		}
		existing, err := q.GetIdempotentRequest(ctx, requestID)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return Result{}, err
		}
		if err == nil {
			switch {
			case existing.UserID != userID || existing.Method != method || existing.Path != path:
				return Result{}, domain.ErrKeyReuse
			}
			return Result{
				Status: int(existing.StatusCode),
				Replay: json.RawMessage(existing.ResponseBody),
			}, nil
		}
	}

	body, status, err := fn(ctx, q)
	if err != nil {
		return Result{}, err
	}

	if requestID != uuid.Nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return Result{}, err
		}
		params := store.CreateIdempotentRequestParams{
			RequestID:    requestID,
			UserID:       userID,
			Method:       method,
			Path:         path,
			StatusCode:   int32(status),
			ResponseBody: raw,
		}
		if incidentID != nil {
			params.IncidentID = pgUUID(*incidentID)
		}
		if _, err := q.CreateIdempotentRequest(ctx, params); err != nil {
			return Result{}, err
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return Result{}, err
	}
	return Result{Status: status, Body: body}, nil
}
