package engine_test

import (
	"context"
	"testing"
	"time"

	"domainengine/internal/apierror"
	"domainengine/internal/models"
	"domainengine/internal/testsupport"
	"domainengine/internal/transfers"
)

// startTransfer sets up two resellers, a domain on the first, and a transfer
// request from the second, returning the created transfer.
func startTransfer(t *testing.T, e *testsupport.Env, name string, startingB int64) (
	tf *models.Transfer, fromR, toR int64, fromC, toC int64, authCode string) {
	t.Helper()
	ctx := context.Background()
	fromR = e.MkReseller(t, "from-"+name, 1_000_000)
	toR = e.MkReseller(t, "to-"+name, startingB)
	fromC = e.MkCustomer(t, fromR, "fc")
	toC = e.MkCustomer(t, toR, "tc")
	d, code := e.Reg(t, name, fromR, fromC)
	tf, _, err := e.Xfer.Request(ctx, e.Clock, transfers.RequestInput{
		DomainName: d.CanonicalName, AuthCode: code,
		ToResellerID: toR, ToCustomerID: toC,
	})
	if err != nil {
		t.Fatalf("request transfer: %v", err)
	}
	authCode = code
	return
}

// TestTransferHappyPath: request freezes fee and sets transferring; seller
// approve starts the 5-day simulated wait; after it elapses the worker
// completes, captures the fee, moves ownership and extends expiry by a year.
func TestTransferHappyPath(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	tf, fromR, toR, _, toC, _ := startTransfer(t, e, "move.example.com", 500_000)
	origExpiry := mustDomain(t, e, "move.example.com").ExpiresAt

	// Fee frozen: toR available -1200, held +1200; domain transferring.
	r := e.Reseller(t, toR)
	if r.BalanceCents != 500_000-1200 || r.HeldCents != 1200 {
		t.Fatalf("after freeze available=%d held=%d", r.BalanceCents, r.HeldCents)
	}
	assertStatus(t, e, "move.example.com", models.StatusTransferring)

	// From-reseller approves the same day.
	e.Clock.Advance(time.Hour)
	if _, err := e.Xfer.Approve(ctx, e.Clock, tf.ID, fromR); err != nil {
		t.Fatalf("approve: %v", err)
	}
	// The gaining reseller cannot approve, and double approval fails.
	if _, err := e.Xfer.Approve(ctx, e.Clock, tf.ID, toR); !isAPI(err, apierror.ErrForbidden) {
		t.Fatalf("to-reseller approve: want forbidden, got %v", err)
	}

	// A tick 4 days later does nothing (5-day wait not elapsed).
	e.Clock.Advance(4 * testsupport.Day)
	if n, err := e.Xfer.ProcessDue(ctx, e.Clock); err != nil || n != 0 {
		t.Fatalf("early tick n=%d err=%v", n, err)
	}
	assertStatus(t, e, "move.example.com", models.StatusTransferring)

	// After the 5-day wait, completion happens.
	e.Clock.Advance(testsupport.Day + time.Nanosecond)
	if n, err := e.Xfer.ProcessDue(ctx, e.Clock); err != nil || n != 1 {
		t.Fatalf("completion tick n=%d err=%v", n, err)
	}
	// Repeated tick is a no-op (restart/double-execution safe).
	if n, err := e.Xfer.ProcessDue(ctx, e.Clock); err != nil || n != 0 {
		t.Fatalf("repeated tick n=%d err=%v", n, err)
	}

	done, err := e.Xfer.Get(ctx, tf.ID)
	if err != nil {
		t.Fatal(err)
	}
	if done.State != models.TransferDone {
		t.Fatalf("state=%s", done.State)
	}
	d, err := e.Domains.Get(ctx, "move.example.com")
	if err != nil {
		t.Fatal(err)
	}
	if d.ResellerID != toR || d.CustomerID != toC {
		t.Fatalf("ownership not moved: reseller=%d customer=%d", d.ResellerID, d.CustomerID)
	}
	if d.Status != models.StatusRegistered {
		t.Fatalf("status=%s", d.Status)
	}
	// Expiry was extended by exactly one year from the pre-transfer expiry.
	if !d.ExpiresAt.Equal(origExpiry.AddDate(1, 0, 0)) {
		t.Fatalf("expiry=%v want %v", d.ExpiresAt, origExpiry.AddDate(1, 0, 0))
	}
	// Fee captured: held back to 0, available was only reduced once (freeze).
	r = e.Reseller(t, toR)
	if r.HeldCents != 0 || r.BalanceCents != 500_000-1200 {
		t.Fatalf("after capture available=%d held=%d", r.BalanceCents, r.HeldCents)
	}
	// Captured-tx ledger movement exists and the old owner was never charged.
	var captureCount int
	if err := e.DB.GetContext(ctx, &captureCount,
		`SELECT count(*) FROM billing_transactions WHERE kind='transfer_capture' AND reseller_id=$1`,
		toR); err != nil {
		t.Fatal(err)
	}
	if captureCount != 1 {
		t.Fatalf("captures=%d", captureCount)
	}
	if got := e.Reseller(t, fromR).HeldCents; got != 0 {
		t.Fatalf("losing reseller held=%d, want 0", got)
	}
}

// TestTransferRejectReleasesFreeze: rejection returns the domain to registered
// and releases the frozen fee in full.
func TestTransferRejectReleasesFreeze(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	tf, fromR, toR, _, _, _ := startTransfer(t, e, "rej.example.com", 300_000)

	if _, err := e.Xfer.Reject(ctx, e.Clock, tf.ID, fromR); err != nil {
		t.Fatalf("reject: %v", err)
	}
	r := e.Reseller(t, toR)
	if r.BalanceCents != 300_000 || r.HeldCents != 0 {
		t.Fatalf("after reject available=%d held=%d", r.BalanceCents, r.HeldCents)
	}
	assertStatus(t, e, "rej.example.com", models.StatusRegistered)
	done, _ := e.Xfer.Get(ctx, tf.ID)
	if done.State != models.TransferRejected {
		t.Fatalf("state=%s", done.State)
	}
}

// TestTransferCancelReleasesFreeze: the gaining reseller can cancel while the
// request is pending and gets the freeze back.
func TestTransferCancelReleasesFreeze(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	tf, _, toR, _, _, _ := startTransfer(t, e, "can.example.com", 250_000)

	if _, err := e.Xfer.Cancel(ctx, e.Clock, tf.ID, toR); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	r := e.Reseller(t, toR)
	if r.BalanceCents != 250_000 || r.HeldCents != 0 {
		t.Fatalf("after cancel available=%d held=%d", r.BalanceCents, r.HeldCents)
	}
	assertStatus(t, e, "can.example.com", models.StatusRegistered)
	// Cancel cannot be repeated (state changed).
	if _, err := e.Xfer.Cancel(ctx, e.Clock, tf.ID, toR); !isAPI(err, apierror.ErrInvalidState) {
		t.Fatalf("repeat cancel: want invalid_state, got %v", err)
	}
}

// TestTransferApprovalTimeout: no seller action within 5 days -> failed, freeze
// released, domain returns to registered.
func TestTransferApprovalTimeout(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	tf, fromR, toR, _, _, _ := startTransfer(t, e, "to.example.com", 400_000)

	e.Clock.Advance(5*testsupport.Day + time.Nanosecond)
	if n, err := e.Xfer.ProcessDue(ctx, e.Clock); err != nil || n != 1 {
		t.Fatalf("timeout tick n=%d err=%v", n, err)
	}
	done, _ := e.Xfer.Get(ctx, tf.ID)
	if done.State != models.TransferFailed {
		t.Fatalf("state=%s want failed", done.State)
	}
	r := e.Reseller(t, toR)
	if r.BalanceCents != 400_000 || r.HeldCents != 0 {
		t.Fatalf("after timeout available=%d held=%d", r.BalanceCents, r.HeldCents)
	}
	assertStatus(t, e, "to.example.com", models.StatusRegistered)
	// Losing reseller only paid for the initial registration (1200); nothing
	// moved for the failed transfer.
	if got := e.Reseller(t, fromR).BalanceCents; got != 1_000_000-1200 {
		t.Fatalf("losing reseller balance=%d", got)
	}
}

// TestTransferWrongAuthCode: an incorrect code is rejected and freezes nothing.
func TestTransferWrongAuthCode(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	fromR := e.MkReseller(t, "from-w", 1_000_000)
	toR := e.MkReseller(t, "to-w", 500_000)
	fromC := e.MkCustomer(t, fromR, "fc")
	toC := e.MkCustomer(t, toR, "tc")
	e.Reg(t, "secret.example.com", fromR, fromC)

	_, _, err := e.Xfer.Request(ctx, e.Clock, transfers.RequestInput{
		DomainName: "secret.example.com", AuthCode: "WRONGCODE1234567",
		ToResellerID: toR, ToCustomerID: toC,
	})
	if !isAPI(err, apierror.ErrBadAuthCode) {
		t.Fatalf("want bad_auth_code, got %v", err)
	}
	r := e.Reseller(t, toR)
	if r.BalanceCents != 500_000 || r.HeldCents != 0 {
		t.Fatalf("funds moved despite bad code: %+v", r)
	}
	assertStatus(t, e, "secret.example.com", models.StatusRegistered)
}

// TestTransferInsufficientFreeze: not enough available credit to freeze fails
// atomically and leaves no transfer or domain state change.
func TestTransferInsufficientFreeze(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	fromR := e.MkReseller(t, "from-i", 1_000_000)
	toR := e.MkReseller(t, "to-i", 100) // fee is 1200
	fromC := e.MkCustomer(t, fromR, "fc")
	toC := e.MkCustomer(t, toR, "tc")
	_, code := e.Reg(t, "poor.example.com", fromR, fromC)

	_, _, err := e.Xfer.Request(ctx, e.Clock, transfers.RequestInput{
		DomainName: "poor.example.com", AuthCode: code,
		ToResellerID: toR, ToCustomerID: toC,
	})
	if !isAPI(err, apierror.ErrInsufficientFunds) {
		t.Fatalf("want insufficient_funds, got %v", err)
	}
	var n int
	if err := e.DB.GetContext(ctx, &n, `SELECT count(*) FROM transfers`); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("no transfer row should exist, got %d", n)
	}
	assertStatus(t, e, "poor.example.com", models.StatusRegistered)
}

// TestPriceChangeDoesNotAffectAcceptedTransfer: the frozen fee is snapshotted
// at request time, so an admin price change mid-transfer never rewrites it.
func TestPriceChangeDoesNotAffectAcceptedTransfer(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	tf, fromR, toR, _, _, _ := startTransfer(t, e, "priced.example.com", 900_000)
	if tf.TransferCents != 1200 {
		t.Fatalf("snapshot fee=%d want 1200", tf.TransferCents)
	}
	// Admin doubles the transfer AND register prices after acceptance.
	if _, err := e.Prices.Set(ctx, "com", 2400, 1500, 20000, 9999); err != nil {
		t.Fatal(err)
	}
	e.Clock.Advance(time.Hour)
	if _, err := e.Xfer.Approve(ctx, e.Clock, tf.ID, fromR); err != nil {
		t.Fatal(err)
	}
	e.Clock.Advance(5*testsupport.Day + time.Nanosecond)
	if _, err := e.Xfer.ProcessDue(ctx, e.Clock); err != nil {
		t.Fatal(err)
	}
	// Gaining reseller still only ever paid the original 1200.
	r := e.Reseller(t, toR)
	if r.BalanceCents != 900_000-1200 || r.HeldCents != 0 {
		t.Fatalf("after completion available=%d held=%d (want %d/0)",
			r.BalanceCents, r.HeldCents, 900_000-1200)
	}
	// New registrations use the new price.
	c2 := e.MkCustomer(t, toR, "c2")
	res, err := e.Domains.Register(ctx, e.Clock, domainsReq("newprice.example.com", toR, c2))
	if err != nil {
		t.Fatal(err)
	}
	if res.ChargeTx.AmountCents != -2400 {
		t.Fatalf("new register charge=%d want -2400 (new price)", res.ChargeTx.AmountCents)
	}
}
