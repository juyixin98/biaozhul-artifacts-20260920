// Package idem contains the idempotent business service: the protocol that
// turns the txn store's claim/commit state machine into concrete request
// handling for POST /v1/orders.
//
// Ambiguity policy (deliberate, and documented in the README):
//
//   - Gateway success  -> commit result + side effect atomically.
//   - Gateway definite error (4xx/500) -> mark the key failed; the client
//     may retry immediately and the attempt is re-executed.
//   - Gateway AMBIGUOUS result (timeout/reset) with an end-to-end
//     idempotency key forwarded -> retry the gateway once with the SAME
//     key; the gateway deduplicates, so a charge that happened in the lost
//     response is not repeated. Success afterwards commits normally.
//   - Ambiguous without a forwarded key -> NEVER blindly retry (that could
//     double-charge), and never commit. The key stays "processing" and the
//     caller gets a clear 503 requiring reconciliation.
package idem

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"idemresp/internal/client"
	"idemresp/internal/clock"
	"idemresp/internal/digest"
	"idemresp/internal/txn"
)

// GatewayCaller is the subset of *client.Client the service needs.
type GatewayCaller interface {
	Charge(ctx context.Context, attempt int, req client.ChargeRequest) client.Result
}

// Config configures a Service.
type Config struct {
	DB    *txn.DB
	GW    GatewayCaller
	Clk   clock.Clock
	Lease time.Duration // processing-claim lease before a crash may be retried
	// AmbiguousRetries is how many extra gateway attempts are allowed
	// after an ambiguous result when an end-to-end key is forwarded.
	AmbiguousRetries int
	Logf             func(format string, args ...any)
	// Crash, if set, is invoked for a matching Input.CrashPoint. In the
	// server it calls os.Exit to simulate a real crash; in unit tests it
	// can just record the stage.
	Crash func(stage string)
}

// Service executes idempotent order requests.
type Service struct {
	cfg Config
}

// New builds a Service.
func New(cfg Config) *Service {
	if cfg.Lease == 0 {
		cfg.Lease = 30 * time.Second
	}
	if cfg.AmbiguousRetries == 0 {
		cfg.AmbiguousRetries = 1
	}
	if cfg.Logf == nil {
		cfg.Logf = func(string, ...any) {}
	}
	if cfg.Clk == nil {
		cfg.Clk = clock.Real{}
	}
	return &Service{cfg: cfg}
}

// Input is a transport-neutral request to execute.
type Input struct {
	Key        string
	Method     string
	Path       string
	Body       []byte
	ForwardKey bool   // forward Idempotency-Key to the gateway
	Fault      string // fault instruction for the fake gateway

	// PreCommitDelay sleeps after a successful gateway call but BEFORE the
	// atomic commit; PostCommitDelay sleeps after commit but before the
	// response is handed to the transport. They model disconnect windows.
	PreCommitDelay  time.Duration
	PostCommitDelay time.Duration

	// CrashPoint, when set, invokes Config.Crash at that point. Used only
	// by tests (enabled via a server flag) to simulate a hard process
	// crash immediately before/after the atomic commit.
	CrashPoint string
}

// Crash point names.
const (
	CrashBeforeCommit = "before-commit"
	CrashAfterCommit  = "after-commit"
)

// Header names returned to transport.
const (
	HeaderReplay     = "Idempotent-Replay"
	HeaderKey        = "Idempotency-Key"
	HeaderRetryAfter = "Retry-After"
)

// Output is the transport-neutral result.
type Output struct {
	Status   int
	Body     []byte
	Replayed bool
	// InProgress is set on the "request still processing" response.
	InProgress bool
	// RetryAfter carries a suggested delay when InProgress.
	RetryAfter time.Duration
}

// Execute runs the idempotency protocol. It always returns an Output;
// failures are encoded as HTTP-shaped responses, never as a Go error, so
// the transport cannot accidentally send an empty reply for a known case.
func (s *Service) Execute(ctx context.Context, in Input) Output {
	reqHash, err := digest.Of(in.Method, in.Path, in.Body)
	if err != nil {
		return errOutput(http.StatusBadRequest, "invalid_request", err.Error())
	}

	res, err := s.cfg.DB.Acquire(in.Key, in.Method, in.Path, reqHash, s.cfg.Lease)
	switch {
	case errors.Is(err, txn.ErrConflict):
		return errOutput(http.StatusConflict, "idempotency_key_conflict",
			"this Idempotency-Key was already used with a different request payload; "+
				"reuse the exact same request or choose a new key")
	case errors.Is(err, txn.ErrInProgress):
		return s.inProgressOutput(res.Existing)
	case err != nil:
		return errOutput(http.StatusInternalServerError, "internal_error", err.Error())
	}

	if !res.Created {
		// Completed earlier: replay the stored response verbatim.
		return Output{
			Status:   statusOrOK(res.Existing.StatusCode),
			Body:     append([]byte(nil), res.Existing.ResponseBody...),
			Replayed: true,
		}
	}

	// Fresh claim: execute exactly once.
	return s.runOnce(ctx, in)
}

func (s *Service) runOnce(ctx context.Context, in Input) Output {
	var orderReq struct {
		Amount    int    `json:"amount"`
		Currency  string `json:"currency"`
		Reference string `json:"reference"`
	}
	if err := json.Unmarshal(in.Body, &orderReq); err != nil {
		_ = s.cfg.DB.Fail(in.Key, err)
		return errOutput(http.StatusBadRequest, "invalid_request", "body is not valid JSON: "+err.Error())
	}
	if orderReq.Amount <= 0 {
		_ = s.cfg.DB.Fail(in.Key, errors.New("amount must be positive"))
		return errOutput(http.StatusBadRequest, "invalid_request", "amount must be a positive integer")
	}
	if orderReq.Currency == "" {
		_ = s.cfg.DB.Fail(in.Key, errors.New("currency is required"))
		return errOutput(http.StatusBadRequest, "invalid_request", "currency is required")
	}

	charge := client.ChargeRequest{
		Amount:    orderReq.Amount,
		Currency:  orderReq.Currency,
		Reference: orderReq.Reference,
		Fault:     in.Fault,
	}
	if in.ForwardKey {
		charge.IdempotencyKey = in.Key
	}

	// Attempt 1, then (only with an end-to-end key) safe retries after an
	// ambiguous transport result.
	var last client.Result
	for attempt := 1; attempt <= 1+s.cfg.AmbiguousRetries; attempt++ {
		if attempt > 1 && !in.ForwardKey {
			break // keyless: retrying ambiguity can double-charge
		}
		last = s.cfg.GW.Charge(ctx, attempt, charge)
		s.cfg.Logf("key=%s gateway attempt=%d outcome=%s", in.Key, attempt, last.Outcome)
		if last.Outcome != client.OutcomeAmbiguous || !in.ForwardKey {
			break
		}
	}

	switch last.Outcome {
	case client.OutcomeFailed:
		_ = s.cfg.DB.Fail(in.Key, last.Err)
		return errOutput(http.StatusBadGateway, "upstream_failure",
			"payment gateway rejected the request; the request may be retried with the same key")
	case client.OutcomeAmbiguous:
		// Money may have moved. No local side effect is committed; the
		// claim stays "processing" pending reconciliation.
		s.cfg.Logf("key=%s AMBIGUOUS outcome, leaving claim processing", in.Key)
		return errOutput(http.StatusServiceUnavailable, "ambiguous_outcome",
			"gateway result is unknown (timeout/reset); not retrying without an end-to-end "+
				"idempotency key, no local side effect recorded; operator reconciliation required")
	}

	// Definite success.
	orderID := deterministicID("ord_", in.Key)
	order := txn.Order{
		ID:       orderID,
		Amount:   orderReq.Amount,
		Currency: orderReq.Currency,
		Status:   "paid",
	}
	resp := map[string]any{
		"order": map[string]any{
			"id":       orderID,
			"amount":   orderReq.Amount,
			"currency": orderReq.Currency,
			"status":   "paid",
		},
		"charge":  last.Charge,
		"deduped": last.HTTPStatus == http.StatusOK,
	}
	respBody, _ := json.MarshalIndent(resp, "", "  ")

	if in.PreCommitDelay > 0 {
		s.cfg.Logf("key=%s pre-commit delay %s (disconnect window)", in.Key, in.PreCommitDelay)
		select {
		case <-time.After(in.PreCommitDelay):
		case <-ctx.Done():
			// Even if the caller vanished, the detached context keeps the
			// work bounded; on its cancellation we still do not commit.
			s.cfg.Logf("key=%s context done before commit: %v", in.Key, ctx.Err())
			return errOutput(http.StatusServiceUnavailable, "aborted", "request ended before commit")
		}
	}

	ledgers := []txn.LedgerEntryInput{{
		Kind:    "charge",
		Detail:  "gateway charge " + last.Charge.ID,
		Amount:  orderReq.Amount,
		OrderID: orderID,
	}}
	if in.CrashPoint == CrashBeforeCommit && s.cfg.Crash != nil {
		s.cfg.Logf("key=%s CRASH before commit (side effect not yet durable)", in.Key)
		s.cfg.Crash(CrashBeforeCommit)
	}
	if err := s.cfg.DB.Commit(in.Key, http.StatusCreated, respBody, []txn.Order{order}, ledgers); err != nil {
		return errOutput(http.StatusInternalServerError, "commit_failed", err.Error())
	}
	s.cfg.Logf("key=%s committed: order=%s charge=%s", in.Key, orderID, last.Charge.ID)

	if in.CrashPoint == CrashAfterCommit && s.cfg.Crash != nil {
		s.cfg.Logf("key=%s CRASH after commit (side effect durable, response not sent)", in.Key)
		s.cfg.Crash(CrashAfterCommit)
	}

	if in.PostCommitDelay > 0 {
		s.cfg.Logf("key=%s post-commit delay %s (disconnect window)", in.Key, in.PostCommitDelay)
		select {
		case <-time.After(in.PostCommitDelay):
		case <-ctx.Done():
			// The side effect is already durable. Signal the caller to
			// retry; the retry will replay this exact response.
			s.cfg.Logf("key=%s context done after commit: %v", in.Key, ctx.Err())
		}
	}
	return Output{Status: http.StatusCreated, Body: respBody}
}

func (s *Service) inProgressOutput(row txn.KeyRow) Output {
	retry := s.cfg.Lease
	if !row.LockedAt.IsZero() {
		elapsed := s.cfg.Clk.Now().Sub(row.LockedAt)
		if elapsed < retry {
			retry = retry - elapsed
		}
	}
	return Output{
		Status:     http.StatusConflict,
		InProgress: true,
		RetryAfter: retry,
		Body: mustJSON(map[string]any{
			"error":           "request_in_progress",
			"idempotency_key": row.Key,
			"attempts":        row.Attempts,
			"message":         "an identical request is being processed; retry with the same Idempotency-Key to get the stored response",
		}),
	}
}

func errOutput(status int, code, msg string) Output {
	return Output{Status: status, Body: mustJSON(map[string]any{
		"error":   code,
		"message": msg,
	})}
}

func statusOrOK(code int) int {
	if code == 0 {
		return http.StatusOK
	}
	return code
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		return []byte(`{"error":"internal_error"}`)
	}
	return b
}

func deterministicID(prefix, key string) string {
	sum := sha256.Sum256([]byte(key))
	return prefix + hex.EncodeToString(sum[:])[:16]
}
