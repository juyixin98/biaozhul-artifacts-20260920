package integration

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
)

// TestStateTransitions covers the allowed and forbidden lifecycle moves.
func TestStateTransitions(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Lifecycle LLC")

	t.Run("capture after void rejected", func(t *testing.T) {
		a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
			MerchantID: m.ID, AmountCents: 1000, CardLast4: "1111",
		}, idem("k1"))
		require.NoError(t, err)
		_, _, err = e.svc.Void(ctx, op, a.ID, idem("k2"))
		require.NoError(t, err)
		_, _, err = e.svc.Capture(ctx, op, service.CaptureInput{PaymentID: a.ID}, idem("k3"))
		assert.ErrorIs(t, err, domain.ErrInvalidState)
	})

	t.Run("void after capture rejected", func(t *testing.T) {
		p := e.captureAuth(t, op, m.ID, 2000)
		_, _, err := e.svc.Void(ctx, op, p.ID, idem("k4"))
		assert.ErrorIs(t, err, domain.ErrInvalidState)
	})

	t.Run("capture after 24h window rejected", func(t *testing.T) {
		a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
			MerchantID: m.ID, AmountCents: 3000, CardLast4: "2222",
		}, idem("k5"))
		require.NoError(t, err)
		e.clock.Advance(24*time.Hour + time.Second)
		_, _, err = e.svc.Capture(ctx, op, service.CaptureInput{PaymentID: a.ID}, idem("k6"))
		assert.ErrorIs(t, err, domain.ErrAuthExpired)
		_, _, err = e.svc.Void(ctx, op, a.ID, idem("k7"))
		assert.ErrorIs(t, err, domain.ErrAuthExpired)
	})

	t.Run("refund of authorized payment rejected", func(t *testing.T) {
		a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
			MerchantID: m.ID, AmountCents: 4000, CardLast4: "3333",
		}, idem("k8"))
		require.NoError(t, err)
		_, _, err = e.svc.Refund(ctx, op, service.RefundInput{PaymentID: a.ID, AmountCents: 1}, idem("k9"))
		assert.ErrorIs(t, err, domain.ErrInvalidState)
	})

	t.Run("partial capture and partial refund", func(t *testing.T) {
		e.clock.Advance(-25 * time.Hour) // move back inside auth windows
		a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
			MerchantID: m.ID, AmountCents: 10000, CardLast4: "4444",
		}, idem("k10"))
		require.NoError(t, err)
		p, _, err := e.svc.Capture(ctx, op, service.CaptureInput{
			PaymentID: a.ID, CaptureCents: 6000,
		}, idem("k11"))
		require.NoError(t, err)
		assert.Equal(t, int64(6000), p.CapturedCents)
		assert.Equal(t, int64(204), p.FeeCents) // 174 + 30

		r, _, err := e.svc.Refund(ctx, op, service.RefundInput{
			PaymentID: p.ID, AmountCents: 2000, Reason: "customer request",
		}, idem("k12"))
		require.NoError(t, err)
		// proportional release: 2.9% of 6000 = 174; 2.9% of 4000 = 116; delta 58
		assert.Equal(t, int64(58), r.FeeRefundCents)
		assert.Equal(t, int64(1942), r.NetCents)

		got, err := e.svc.GetPayment(ctx, op, p.ID)
		require.NoError(t, err)
		assert.Equal(t, domain.StatusPartiallyRefunded, got.Status)
		assert.Equal(t, int64(2000), got.RefundedCents)
	})

	t.Run("over-capture rejected", func(t *testing.T) {
		a, _, err := e.svc.Authorize(ctx, op, service.AuthorizeInput{
			MerchantID: m.ID, AmountCents: 500, CardLast4: "5555",
		}, idem("k13"))
		require.NoError(t, err)
		_, _, err = e.svc.Capture(ctx, op, service.CaptureInput{
			PaymentID: a.ID, CaptureCents: 600,
		}, idem("k14"))
		assert.ErrorIs(t, err, domain.ErrCaptureTooLarge)
	})

	t.Run("refund beyond captured rejected", func(t *testing.T) {
		p := e.captureAuth(t, op, m.ID, 1000)
		_, _, err := e.svc.Refund(ctx, op, service.RefundInput{
			PaymentID: p.ID, AmountCents: 600,
		}, idem("k15"))
		require.NoError(t, err)
		_, _, err = e.svc.Refund(ctx, op, service.RefundInput{
			PaymentID: p.ID, AmountCents: 500,
		}, idem("k16"))
		assert.ErrorIs(t, err, domain.ErrRefundTooLarge)
	})

	t.Run("post-settlement refund window 90 days", func(t *testing.T) {
		p := e.captureAuth(t, op, m.ID, 3000)
		day := e.clock.Now()
		_, err := e.svc.SettleDay(ctx, e.admin, m.ID, day)
		require.NoError(t, err)

		// Within 90 days: allowed.
		e.clock.Advance(90*24*time.Hour - time.Hour)
		r, _, err := e.svc.Refund(ctx, op, service.RefundInput{
			PaymentID: p.ID, AmountCents: 1000,
		}, idem("k17"))
		require.NoError(t, err)
		assert.Equal(t, int64(1000), r.AmountCents)

		// After 90 days: rejected.
		e.clock.Advance(2 * time.Hour)
		_, _, err = e.svc.Refund(ctx, op, service.RefundInput{
			PaymentID: p.ID, AmountCents: 500,
		}, idem("k18"))
		assert.ErrorIs(t, err, domain.ErrRefundWindowClosed)
	})
}

// TestRBAC verifies role restrictions and merchant scoping.
func TestRBAC(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m1, op1, aud1 := e.createMerchantWithKeys(t, "Merchant One")
	m2, op2, _ := e.createMerchantWithKeys(t, "Merchant Two")

	p := e.captureAuth(t, op1, m1.ID, 1000)

	// Auditor is read-only.
	_, _, err := e.svc.Authorize(ctx, aud1, service.AuthorizeInput{
		MerchantID: m1.ID, AmountCents: 1, CardLast4: "0000",
	}, idem("rbac1"))
	assert.ErrorIs(t, err, domain.ErrForbidden)

	// Auditor can read.
	_, err = e.svc.GetPayment(ctx, aud1, p.ID)
	assert.NoError(t, err)

	// Operator cannot administer merchants.
	_, err = e.svc.CreateMerchant(ctx, op1, service.CreateMerchantInput{Name: "x"})
	assert.ErrorIs(t, err, domain.ErrForbidden)

	// Operator scoped to own merchant.
	_, err = e.svc.GetPayment(ctx, op2, p.ID)
	assert.ErrorIs(t, err, domain.ErrForbidden)
	_, err = e.svc.GetMerchant(ctx, op2, m1.ID)
	assert.ErrorIs(t, err, domain.ErrForbidden)
	_ = m2
}
