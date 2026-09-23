package integration

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"vci/internal/clock"
	"vci/internal/domain"
	"vci/internal/service"
)

// TestConcurrentRevocation fires many goroutines revoking the same
// credential at once. Exactly one must win; every loser must see
// ErrConflict; the database must hold exactly one revocation event and the
// snapshot must be exactly issuance+1 (no gaps burned by losing attempts).
func TestConcurrentRevocation(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	svc := service.New(st, clock.System{})

	iss, err := svc.CreateIssuer(ctx, "concurrency-ca")
	if err != nil {
		t.Fatal(err)
	}
	nb := time.Now().Add(-time.Minute)
	c := issueFor(t, svc, iss.Issuer.ID, "subj", "p", nb, time.Now().Add(time.Hour))

	const n = 32
	var wg sync.WaitGroup
	var mu sync.Mutex
	var wins, conflicts, others int
	wg.Add(n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			<-start
			_, err := svc.Revoke(ctx, service.RevokeRequest{
				CredentialID: c.ID,
				Reason:       "race",
			})
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				wins++
			case errors.Is(err, domain.ErrConflict):
				conflicts++
			default:
				others++
				t.Errorf("unexpected revoke error: %v", err)
			}
		}()
	}
	close(start)
	wg.Wait()

	if wins != 1 {
		t.Fatalf("winners=%d, want exactly 1 (conflicts=%d others=%d)", wins, conflicts, others)
	}
	if conflicts != n-1 {
		t.Fatalf("conflicts=%d, want %d", conflicts, n-1)
	}

	// Exactly one row in revocations.
	pool := poolFromStore(t, st)
	var rows int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM revocations WHERE credential_id=$1`, c.ID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 1 {
		t.Fatalf("revocation rows=%d, want 1", rows)
	}

	// Losers must not have burned snapshot numbers: post-revocation
	// verification reports issuance snapshot + 1.
	res, err := svc.VerifyCredential(ctx, service.VerifyRequest{CredentialID: c.ID})
	if err != nil {
		t.Fatal(err)
	}
	if res.Verdict != domain.VerdictRevoked {
		t.Fatalf("verdict=%s, want REVOKED", res.Verdict)
	}
	if res.Snapshot != c.Snapshot+1 {
		t.Fatalf("snapshot after revoke race=%d, want exactly %d (no gaps from losers)",
			res.Snapshot, c.Snapshot+1)
	}
}

// TestRevokeAndVerifySameSnapshot is the headline property: after a
// revoke, verification returns the SAME snapshot number as the revoke
// response, and a second verify is a cache hit that still carries it.
func TestRevokeAndVerifySameSnapshot(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	clk := &fakeClock{t: time.Now()}
	svc := service.New(st, clk)

	iss, err := svc.CreateIssuer(ctx, "snap-ca")
	if err != nil {
		t.Fatal(err)
	}
	c := issueFor(t, svc, iss.Issuer.ID, "s", "p", clk.t.Add(-time.Minute), clk.t.Add(time.Hour))

	before, err := svc.VerifyCredential(ctx, service.VerifyRequest{CredentialID: c.ID})
	if err != nil || before.Verdict != domain.VerdictValid {
		t.Fatalf("before revoke: %v %s", err, before.Verdict)
	}
	rev, err := svc.Revoke(ctx, service.RevokeRequest{CredentialID: c.ID, Reason: "x"})
	if err != nil {
		t.Fatal(err)
	}
	after, err := svc.VerifyCredential(ctx, service.VerifyRequest{CredentialID: c.ID})
	if err != nil {
		t.Fatal(err)
	}
	if after.CacheHit {
		t.Fatal("post-revocation verify was served from cache")
	}
	if after.Snapshot != rev.Snapshot {
		t.Fatalf("verify snapshot %d != revoke snapshot %d", after.Snapshot, rev.Snapshot)
	}
	if after.Verdict != domain.VerdictRevoked {
		t.Fatalf("verdict %s", after.Verdict)
	}
	// Repeated verify: cache hit at the new snapshot, still revoked.
	after2, err := svc.VerifyCredential(ctx, service.VerifyRequest{CredentialID: c.ID})
	if err != nil {
		t.Fatal(err)
	}
	if !after2.CacheHit || after2.Snapshot != rev.Snapshot || after2.Verdict != domain.VerdictRevoked {
		t.Fatalf("cached post-revocation verify wrong: %+v", after2)
	}
}

// TestRotationOverPostgres covers signing on both sides of a real
// rotation, historical replay and "issue fails while no key is active".
func TestRotationOverPostgres(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	now := time.Now()
	clk := &fakeClock{t: now}
	svc := service.New(st, clk)

	iss, err := svc.CreateIssuer(ctx, "pg-rot-ca")
	if err != nil {
		t.Fatal(err)
	}
	c1 := issueFor(t, svc, iss.Issuer.ID, "a", "p", now, now.Add(24*time.Hour))

	clk.t = now.Add(2 * time.Hour)
	rot, err := svc.RotateKey(ctx, iss.Issuer.ID, true)
	if err != nil {
		t.Fatal(err)
	}
	if rot.Key.Seq != 2 {
		t.Fatalf("seq=%d", rot.Key.Seq)
	}
	c2 := issueFor(t, svc, iss.Issuer.ID, "b", "p", clk.t, clk.t.Add(24*time.Hour))
	if c2.Kid != rot.Key.ID {
		t.Fatalf("new credential not signed by rotated key")
	}

	// c1 verifies historically and after rotation.
	at := now.Add(30 * time.Minute)
	if r := verifyAt(t, svc, c1.ID, "p", at); r.Verdict != domain.VerdictValid {
		t.Fatalf("c1 historical: %s (%s)", r.Verdict, r.Reason)
	}
	if r := verifyAt(t, svc, c1.ID, "p", now.Add(90*time.Minute)); r.Verdict != domain.VerdictValid {
		t.Fatalf("c1 after rotation: %s (%s)", r.Verdict, r.Reason)
	}
	// c2 cannot be replayed before its issuance instant (issued at ~now+2h).
	if r := verifyAt(t, svc, c2.ID, "p", now.Add(119*time.Minute)); r.Verdict != domain.VerdictUnknown {
		t.Fatalf("c2 pre-issuance replay: %s", r.Verdict)
	}
	// Right after issuance (and inside its validity window): VALID.
	if r := verifyAt(t, svc, c2.ID, "p", now.Add(121*time.Minute)); r.Verdict != domain.VerdictValid {
		t.Fatalf("c2 after issuance: %s (%s)", r.Verdict, r.Reason)
	}

	// Retire everything: issuance must fail until a new rotation.
	if _, err := svc.RetireKey(ctx, iss.Issuer.ID); err != nil {
		t.Fatal(err)
	}
	_, err = issueForErr(svc, iss.Issuer.ID, "c", "p", clk.t, clk.t.Add(24*time.Hour))
	if !errors.Is(err, domain.ErrNoActiveKey) {
		t.Fatalf("issue while retired: %v", err)
	}
	if _, err := svc.RotateKey(ctx, iss.Issuer.ID, true); err != nil {
		t.Fatal(err)
	}
	c3 := issueFor(t, svc, iss.Issuer.ID, "c", "p", clk.t, clk.t.Add(24*time.Hour))
	if r := verifyAt(t, svc, c3.ID, "p", clk.t); r.Verdict != domain.VerdictValid {
		t.Fatalf("c3 after re-key: %s (%s)", r.Verdict, r.Reason)
	}
}

// TestSnapshotMonotonicAndReplay exercises the snapshot head across many
// appends and checks backdated revocation replay against real storage.
func TestSnapshotMonotonicAndReplay(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	clk := &fakeClock{t: time.Now()}
	// Credentials are issued at clk.t but valid over an absolute historical
	// window, so backdated replay times are comparable to issuance.
	base := clk.t.Add(-24 * time.Hour)
	svc := service.New(st, clk)

	iss, err := svc.CreateIssuer(ctx, "many-ca")
	if err != nil {
		t.Fatal(err)
	}
	var last int64
	for i := 0; i < 5; i++ {
		c := issueFor(t, svc, iss.Issuer.ID, "sub", "p", base, base.Add(48*time.Hour))
		if c.Snapshot <= last {
			t.Fatalf("snapshot not monotonic: %d <= %d", c.Snapshot, last)
		}
		last = c.Snapshot
	}

	// Backdated revoke on the most recent credential.
	c := issueFor(t, svc, iss.Issuer.ID, "target", "p", base, base.Add(48*time.Hour))
	eff := base.Add(2 * time.Hour)
	if _, err := svc.Revoke(ctx, service.RevokeRequest{CredentialID: c.ID, Reason: "late", EffectiveAt: &eff}); err != nil {
		t.Fatal(err)
	}
	if r := verifyAt(t, svc, c.ID, "p", base.Add(time.Hour)); r.Verdict != domain.VerdictUnknown {
		t.Fatalf("before issuance: %s, want UNKNOWN", r.Verdict)
	}
	// Replay at an instant after issuance AND after the backdated
	// effective_at: REVOKED.
	if r := verifyAt(t, svc, c.ID, "p", clk.t.Add(time.Hour)); r.Verdict != domain.VerdictRevoked {
		t.Fatalf("after issuance+effective: %s (%s)", r.Verdict, r.Reason)
	}
	// Purpose mismatch over Postgres (on a non-revoked credential).
	c2 := issueFor(t, svc, iss.Issuer.ID, "target2", "correct", base, base.Add(48*time.Hour))
	if r := verifyAt(t, svc, c2.ID, "wrong", clk.t); r.Verdict != domain.VerdictPurposeMismatch {
		t.Fatalf("purpose: %s (%s)", r.Verdict, r.Reason)
	}
}

// TestExpiryBoundaryPostgres repeats the exact-boundary semantics against
// the real database row reading path.
func TestExpiryBoundaryPostgres(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	clk := &fakeClock{t: time.Now()}
	nb := clk.t
	exp := nb.Add(2 * time.Hour) // expiry safely in the future relative to "now"
	svc := service.New(st, clk)
	iss, err := svc.CreateIssuer(ctx, "exp-ca")
	if err != nil {
		t.Fatal(err)
	}
	c := issueFor(t, svc, iss.Issuer.ID, "s", "p", nb, exp)
	if r := verifyAt(t, svc, c.ID, "p", nb); r.Verdict != domain.VerdictValid {
		t.Fatalf("at not_before: %s (%s)", r.Verdict, r.Reason)
	}
	// Postgres timestamptz stores microsecond precision; stay one
	// microsecond away from the boundary instead of one nanosecond.
	if r := verifyAt(t, svc, c.ID, "p", exp.Add(-time.Microsecond)); r.Verdict != domain.VerdictValid {
		t.Fatalf("1us before expiry: %s (%s)", r.Verdict, r.Reason)
	}
	if r := verifyAt(t, svc, c.ID, "p", exp); !strings.Contains(string(r.Verdict), "EXPIRED") {
		t.Fatalf("at expiry: %s", r.Verdict)
	}
}

// --- helpers ---

type fakeClock struct{ t time.Time }

func (f *fakeClock) Now() time.Time { return f.t.UTC() }

func issueFor(t *testing.T, svc *service.Service, issuer, subj, purpose string, nb, exp time.Time) service.CredentialView {
	t.Helper()
	c, err := issueForErr(svc, issuer, subj, purpose, nb, exp)
	if err != nil {
		t.Fatalf("issue: %v", err)
	}
	return c
}

func issueForErr(svc *service.Service, issuer, subj, purpose string, nb, exp time.Time) (service.CredentialView, error) {
	content := []byte(`{"k":"v"}`)
	nbc := nb
	return svc.IssueCredential(context.Background(), service.IssueRequest{
		IssuerID: issuer, Subject: subj, Purpose: purpose,
		NotBefore: &nbc, ExpiresAt: exp, Content: content,
	})
}

func verifyAt(t *testing.T, svc *service.Service, id, purpose string, at time.Time) service.VerifyResult {
	t.Helper()
	r, err := svc.VerifyCredential(context.Background(), service.VerifyRequest{
		CredentialID: id, ExpectedPurpose: purpose, AsOf: &at,
	})
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	return r
}
