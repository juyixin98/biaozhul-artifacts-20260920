package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
)

// Idem carries an idempotency key and the canonical hash of request params.
type Idem struct {
	Key         string
	RequestHash string
}

type actionResult struct {
	resourceID uuid.UUID
	payload    any
}

// runIdempotent executes action inside one serializable transaction together
// with the idempotency bookkeeping, so the stored response and the money
// movement always commit atomically:
//
//   - first use: action runs, its result is stored and returned;
//   - same key + same params: the original result is returned, action does NOT
//     run again (no double ledger posting);
//   - same key + different params: ErrIdempotencyConflict (HTTP 409);
//   - concurrent first uses with the same key serialize: exactly one runs the
//     action, the other replays its result.
//
// scope identifies the idempotency namespace: the target merchant id, or
// uuid.Nil for platform-level actions (merchant creation).
func (s *Service) runIdempotent(
	ctx context.Context,
	actor Actor,
	endpoint string,
	scope uuid.UUID,
	idem Idem,
	action func(ctx context.Context, q *db.Queries) (actionResult, error),
) (payload json.RawMessage, replay bool, err error) {
	if idem.Key == "" {
		return nil, false, fmt.Errorf("%w: Idempotency-Key header required", domain.ErrValidation)
	}

	runErr := s.retryTx(ctx, func(q *db.Queries) error {
		existing, gerr := q.GetIdempotency(ctx, db.GetIdempotencyParams{
			Key: idem.Key, MerchantID: scope, Endpoint: endpoint,
		})
		if gerr == nil {
			if existing.RequestHash != idem.RequestHash {
				return domain.ErrIdempotencyConflict
			}
			payload = json.RawMessage(existing.ResponseBody)
			replay = true
			return nil
		}
		if !errors.Is(gerr, pgxNoRows) {
			return gerr
		}

		res, aerr := action(ctx, q)
		if aerr != nil {
			return aerr
		}
		body, merr := json.Marshal(res.payload)
		if merr != nil {
			return merr
		}
		var resource *uuid.UUID
		if res.resourceID != uuid.Nil {
			rid := res.resourceID
			resource = &rid
		}
		if _, ierr := q.InsertIdempotency(ctx, db.InsertIdempotencyParams{
			Key:            idem.Key,
			MerchantID:     scope,
			Endpoint:       endpoint,
			RequestHash:    idem.RequestHash,
			ResourceID:     resource,
			ResponseStatus: 200,
			ResponseBody:   body,
		}); ierr != nil {
			var pgErr *pgconn.PgError
			if errors.As(ierr, &pgErr) && pgErr.Code == "23505" {
				// Concurrent request inserted the same key first; fail so the
				// serialization retry reads its stored response.
				return serializationFailure()
			}
			return ierr
		}
		payload = body
		return nil
	})
	if runErr != nil {
		return nil, false, runErr
	}
	return payload, replay, nil
}

// CanonicalRequestHash produces a stable SHA-256 over a JSON request body.
// JSON objects are re-encoded with sorted keys so byte-level formatting
// differences never cause a false conflict; semantically identical payloads
// hash equally.
func CanonicalRequestHash(raw []byte) (string, error) {
	var v any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &v); err != nil {
			return "", fmt.Errorf("%w: body is not valid JSON", domain.ErrValidation)
		}
	}
	canonical, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

// runIdempotentTx is the variant for operations whose idempotency scope
// (merchant) is only known after loading the target resource inside the
// transaction.
//
//	scope : side-effect-free lookup of the merchant id (also enforces 404);
//	run   : the business action (locks rows, posts ledger, mutates state);
//	result: builds the stored response from the mutated state.
//
// Because scope is resolved first, a replay returns the stored response
// WITHOUT re-running run, so a duplicate refund after the first committed is
// never rejected by the (now tighter) balance check.
func (s *Service) runIdempotentTx(
	ctx context.Context,
	actor Actor,
	endpoint string,
	idem Idem,
	scope func(q *db.Queries) (uuid.UUID, error),
	run func(q *db.Queries) error,
	result func() actionResult,
) (json.RawMessage, bool, error) {
	if idem.Key == "" {
		return nil, false, fmt.Errorf("%w: Idempotency-Key header required", domain.ErrValidation)
	}
	var payload json.RawMessage
	replayed := false

	// READ COMMITTED + SELECT ... FOR UPDATE on the payment row gives
	// pessimistic serialization without 40001 read-write-conflict failures
	// under a refund hot-spot: contenders block on the row lock, then see the
	// latest committed state and re-check the limits. The idempotency unique
	// constraint still needs a retry on 23505 (handled below).
	err := s.retryTxIso(ctx, pgx.ReadCommitted, func(q *db.Queries) error {
		merchantID, err := scope(q)
		if err != nil {
			return err
		}
		existing, gerr := q.GetIdempotency(ctx, db.GetIdempotencyParams{
			Key: idem.Key, MerchantID: merchantID, Endpoint: endpoint,
		})
		if gerr == nil {
			if existing.RequestHash != idem.RequestHash {
				return domain.ErrIdempotencyConflict
			}
			payload = json.RawMessage(existing.ResponseBody)
			replayed = true
			return nil
		}
		if !errors.Is(gerr, pgxNoRows) {
			return gerr
		}

		if err := run(q); err != nil {
			return err
		}
		res := result()
		body, err := json.Marshal(res.payload)
		if err != nil {
			return err
		}
		var resource *uuid.UUID
		if res.resourceID != uuid.Nil {
			rid := res.resourceID
			resource = &rid
		}
		if _, err := q.InsertIdempotency(ctx, db.InsertIdempotencyParams{
			Key:            idem.Key,
			MerchantID:     merchantID,
			Endpoint:       endpoint,
			RequestHash:    idem.RequestHash,
			ResourceID:     resource,
			ResponseStatus: 200,
			ResponseBody:   body,
		}); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23505" {
				return serializationFailure()
			}
			return err
		}
		payload = body
		return nil
	})
	if err != nil {
		return nil, false, err
	}
	return payload, replayed, nil
}

// retryTxRaw is retryTx without the WithTx adapter convenience.
func (s *Service) retryTxRaw(ctx context.Context, fn func(q *db.Queries) error) error {
	var lastErr error
	for attempt := 0; attempt < maxSerializationRetries; attempt++ {
		err := storeInTxSerializable(ctx, s.pool, func(tx pgx.Tx) error {
			return fn(s.q.WithTx(tx))
		})
		if err == nil {
			return nil
		}
		lastErr = err
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && (pgErr.Code == "40001" || pgErr.Code == "40P01") {
			continue
		}
		return err
	}
	return fmt.Errorf("transaction conflict after retries: %w", lastErr)
}

// serializationFailure builds a synthetic 40001 error to trigger a retry.
func serializationFailure() error {
	return &pgconn.PgError{Code: "40001", Message: "synthetic: re-read idempotency row"}
}
