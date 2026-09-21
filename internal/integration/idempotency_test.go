package integration

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
)

// TestIdempotencyReplayAndConflict verifies:
//   - same key + same params returns the original result, no duplicate action;
//   - same key + different params is a 409 conflict.
func TestIdempotencyReplayAndConflict(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Idem Inc")

	bodyHash := func(s string) string {
		h, err := service.CanonicalRequestHash([]byte(s))
		require.NoError(t, err)
		return h
	}
	const key = "auth-key-1"
	in := service.AuthorizeInput{MerchantID: m.ID, AmountCents: 1500, CardLast4: "4242"}
	const canonicalBody = `{"amount_cents":1500,"card_last4":"4242"}`

	first, replayed, err := e.svc.Authorize(ctx, op, in, service.Idem{
		Key: key, RequestHash: bodyHash(canonicalBody),
	})
	require.NoError(t, err)
	assert.False(t, replayed)

	second, replayed, err := e.svc.Authorize(ctx, op, in, service.Idem{
		Key: key, RequestHash: bodyHash(canonicalBody),
	})
	require.NoError(t, err)
	assert.True(t, replayed)
	assert.Equal(t, first.ID, second.ID, "replay returns the same payment")

	// Whitespace/key-order differences still hash the same (same fields).
	third, replayed, err := e.svc.Authorize(ctx, op, in, service.Idem{
		Key: key, RequestHash: bodyHash(`{"card_last4": "4242", "amount_cents": 1500}`),
	})
	require.NoError(t, err)
	assert.True(t, replayed)
	assert.Equal(t, first.ID, third.ID)

	// Different amount with the same key -> conflict.
	_, _, err = e.svc.Authorize(ctx, op,
		service.AuthorizeInput{MerchantID: m.ID, AmountCents: 9999, CardLast4: "4242"},
		service.Idem{Key: key, RequestHash: bodyHash(`{"amount_cents":9999,"card_last4":"4242"}`)})
	assert.ErrorIs(t, err, domain.ErrIdempotencyConflict)

	// Only one payment was actually created.
	rows, err := e.svc.ListPayments(ctx, op, m.ID, 50, 0)
	require.NoError(t, err)
	assert.Len(t, rows, 1)
}

// TestIdempotencyRefundDuplicate: replaying a refund key after the first
// committed must return the original refund and never post twice.
func TestIdempotencyRefundDuplicate(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Dup Refund Co")
	p := e.captureAuth(t, op, m.ID, 5000)

	idem := service.Idem{Key: "refund-1", RequestHash: "x"}
	r1, replayed, err := e.svc.Refund(ctx, op, service.RefundInput{
		PaymentID: p.ID, AmountCents: 2000,
	}, idem)
	require.NoError(t, err)
	assert.False(t, replayed)

	r2, replayed, err := e.svc.Refund(ctx, op, service.RefundInput{
		PaymentID: p.ID, AmountCents: 2000,
	}, idem)
	require.NoError(t, err)
	assert.True(t, replayed)
	assert.Equal(t, r1.ID, r2.ID)

	got, err := e.svc.GetPayment(ctx, op, p.ID)
	require.NoError(t, err)
	assert.Equal(t, int64(2000), got.RefundedCents, "refund posted exactly once")

	entries, err := e.svc.ListLedgerEntries(ctx, op, "payment", p.ID)
	require.NoError(t, err)
	// capture has 3 balanced legs only; the replay added none.
	assert.Len(t, entries, 3)
}

// TestConcurrentRefundsNoOverRefund fires many partial refunds with distinct
// idempotency keys against one payment concurrently and asserts the total
// never exceeds the captured amount.
func TestConcurrentRefundsNoOverRefund(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Race Refunds")
	p := e.captureAuth(t, op, m.ID, 10000)

	const n = 12
	const each int64 = 1000 // total attempted 12000 > 10000 captured
	var wg sync.WaitGroup
	errs := make([]error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _, errs[i] = e.svc.Refund(ctx, op, service.RefundInput{
				PaymentID: p.ID, AmountCents: each,
			}, idem("race-refund-"+uuid.NewString()))
		}(i)
	}
	wg.Wait()

	var accepted int
	for _, err := range errs {
		switch {
		case err == nil:
			accepted++
		case assertErrIs(err, domain.ErrRefundTooLarge),
			assertErrIs(err, domain.ErrInvalidState):
			// Once the payment is fully refunded, late waiters may see either
			// the cap check or the terminal-state check.
		default:
			t.Fatalf("unexpected refund error: %v", err)
		}
	}
	assert.Equal(t, 10, accepted, "exactly captured/each refunds succeed")

	got, err := e.svc.GetPayment(ctx, op, p.ID)
	require.NoError(t, err)
	assert.Equal(t, int64(10000), got.RefundedCents)
	assert.Equal(t, domain.StatusRefunded, got.Status)

	// Ledger: gateway_cash legs = +10000 capture, -1000*10 refunds = 0
	// (captured cash fully returned; settlement has not run).
	bals, err := e.svc.AccountBalances(ctx, op, m.ID)
	require.NoError(t, err)
	bal := map[string]int64{}
	for _, b := range bals {
		bal[b.Code] = b.Balance
	}
	assert.Equal(t, int64(0), bal["gateway_cash"], "all captured cash was refunded")
}

// TestConcurrentCapturesSameAuth: duplicate capture attempts (distinct keys)
// must never double post; only one can win.
func TestConcurrentCapturesSameAuth(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Race Capture")
	a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
		MerchantID: m.ID, AmountCents: 4000, CardLast4: "4242",
	}, idem("auth-cap-race"))
	require.NoError(t, err)

	var wg sync.WaitGroup
	errs := make([]error, 5)
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _, errs[i] = e.svc.Capture(ctx, op,
				service.CaptureInput{PaymentID: a.ID},
				idem("cap-race-"+uuid.NewString()))
		}(i)
	}
	wg.Wait()

	ok, bad := 0, 0
	for _, err := range errs {
		switch {
		case err == nil:
			ok++
		case assertErrIs(err, domain.ErrInvalidState):
			bad++
		default:
			t.Fatalf("unexpected capture error: %v", err)
		}
	}
	assert.Equal(t, 1, ok)
	assert.Equal(t, 4, bad)

	entries, err := e.svc.ListLedgerEntries(ctx, op, "payment", a.ID)
	require.NoError(t, err)
	assert.Len(t, entries, 3, "capture posted exactly once")
}

// TestConcurrentVoidAndCapture: exactly one of void/capture wins.
func TestConcurrentVoidAndCapture(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Void Race")
	a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
		MerchantID: m.ID, AmountCents: 777, CardLast4: "4242",
	}, idem("auth-void-race"))
	require.NoError(t, err)

	var wg sync.WaitGroup
	var capErr, voidErr error
	wg.Add(2)
	go func() {
		defer wg.Done()
		_, _, capErr = e.svc.Capture(ctx, op,
			service.CaptureInput{PaymentID: a.ID}, idem("vc-cap"))
	}()
	go func() { defer wg.Done(); _, _, voidErr = e.svc.Void(ctx, op, a.ID, idem("vc-void")) }()
	wg.Wait()

	p, err := e.svc.GetPayment(ctx, op, a.ID)
	require.NoError(t, err)
	if capErr == nil {
		assert.ErrorIs(t, voidErr, domain.ErrInvalidState)
		assert.Equal(t, domain.StatusCaptured, p.Status)
	} else {
		assert.ErrorIs(t, capErr, domain.ErrInvalidState)
		assert.NoError(t, voidErr)
		assert.Equal(t, domain.StatusVoided, p.Status)
	}
}

func assertErrIs(err, target error) bool {
	return errors.Is(err, target)
}

// avoid import cycle complaints by referencing db.
var _ = db.Transaction{}
