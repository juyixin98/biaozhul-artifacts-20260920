package service_test

import (
	"testing"
	"time"

	"vci/internal/domain"
	"vci/internal/service"
)

// TestScheduledRevocationChangesWithTime verifies that a revocation event
// recorded now but effective in the future flips the CURRENT verdict at its
// effective instant purely through passage of time (no further append):
// the snapshot-keyed cache alone cannot express this (head is unchanged),
// so the entry's freshUntil boundary must invalidate it.
func TestScheduledRevocationChangesWithTime(t *testing.T) {
	t0 := time.Date(2026, 12, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("sched-ca")
	c := e.issue(iss.Issuer.ID, "k", "p", t0, t0.Add(24*time.Hour), map[string]any{"v": 1})

	eff := t0.Add(time.Hour)
	if _, err := e.svc.Revoke(e.ctx, service.RevokeRequest{
		CredentialID: c.ID, Reason: "scheduled", EffectiveAt: &eff,
	}); err != nil {
		t.Fatal(err)
	}

	// Current while the revocation is not yet effective.
	r := verify(t, e.svc, c.ID, "p", nil)
	if r.Verdict != domain.VerdictValid {
		t.Fatalf("before effective: %s", r.Verdict)
	}
	// Again: cached VALID is fine while wall time has not moved (mock clock
	// is fixed), head unchanged and we are before freshUntil.
	r = verify(t, e.svc, c.ID, "p", nil)
	if !r.CacheHit {
		t.Fatal("expected cache hit while time stands still pre-effective")
	}

	// Move the clock exactly to the effective instant: the current verdict
	// must become REVOKED without any new append.
	e.clk.Advance(time.Hour)
	r = verify(t, e.svc, c.ID, "p", nil)
	if r.Verdict != domain.VerdictRevoked {
		t.Fatalf("at effective instant: %s (%s)", r.Verdict, r.Reason)
	}
	if r.CacheHit {
		t.Fatal("time-boundary crossing must not be served from cache")
	}

	// Historical replay pre-effective still says VALID.
	before := t0.Add(30 * time.Minute)
	if r := verify(t, e.svc, c.ID, "p", &before); r.Verdict != domain.VerdictValid {
		t.Fatalf("historical pre-effective: %s", r.Verdict)
	}
}

// TestNaturalExpiryChangesWithTime exercises the same freshness mechanism
// for the expiry boundary of an un-revoked credential.
func TestNaturalExpiryChangesWithTime(t *testing.T) {
	t0 := time.Date(2026, 12, 2, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("expiry-ca")
	c := e.issue(iss.Issuer.ID, "k", "p", t0, t0.Add(time.Hour), map[string]any{"v": 1})

	if r := verify(t, e.svc, c.ID, "p", nil); r.Verdict != domain.VerdictValid {
		t.Fatalf("fresh: %s", r.Verdict)
	}
	e.clk.Advance(time.Hour)
	r := verify(t, e.svc, c.ID, "p", nil)
	if r.Verdict != domain.VerdictExpired || r.CacheHit {
		t.Fatalf("after natural expiry: verdict=%s hit=%v, want EXPIRED/no-cache", r.Verdict, r.CacheHit)
	}
}
