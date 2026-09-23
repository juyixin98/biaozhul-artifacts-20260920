package service_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"vci/internal/clock"
	"vci/internal/crypto"
	"vci/internal/domain"
	"vci/internal/service"
	"vci/internal/store/memstore"
)

// testEnv bundles everything a replay test needs with a controllable clock.
type testEnv struct {
	t   *testing.T
	svc *service.Service
	clk *clock.Mock
	ctx context.Context
}

func newEnv(t *testing.T, start time.Time) *testEnv {
	t.Helper()
	clk := clock.NewMock(start)
	st := memstore.New()
	svc := service.New(st, clk)
	return &testEnv{t: t, svc: svc, clk: clk, ctx: context.Background()}
}

func (e *testEnv) issuer(name string) service.CreateIssuerResult {
	e.t.Helper()
	res, err := e.svc.CreateIssuer(e.ctx, name)
	if err != nil {
		e.t.Fatalf("create issuer: %v", err)
	}
	return res
}

func (e *testEnv) issue(issuerID, subject, purpose string, nb, exp time.Time, content map[string]any) service.CredentialView {
	e.t.Helper()
	raw, err := json.Marshal(content)
	if err != nil {
		e.t.Fatal(err)
	}
	nbCopy := nb
	res, err := e.svc.IssueCredential(e.ctx, service.IssueRequest{
		IssuerID: issuerID, Subject: subject, Purpose: purpose,
		NotBefore: &nbCopy, ExpiresAt: exp, Content: raw,
	})
	if err != nil {
		e.t.Fatalf("issue credential: %v", err)
	}
	return res
}

func verify(t *testing.T, svc *service.Service, credID, purpose string, asOf *time.Time) service.VerifyResult {
	t.Helper()
	res, err := svc.VerifyCredential(context.Background(), service.VerifyRequest{
		CredentialID: credID, ExpectedPurpose: purpose, AsOf: asOf,
	})
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	return res
}

func mustB64(t *testing.T, s string) []byte {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// TestExpiryBoundary checks the exact instant semantics: [not_before,
// expires_at) — the expiry instant itself is EXPIRED.
func TestExpiryBoundary(t *testing.T) {
	t0 := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("lab-ca")

	nb := t0.Add(time.Hour)
	exp := t0.Add(2 * time.Hour)
	c := e.issue(iss.Issuer.ID, "subject-42", "gate-access", nb, exp,
		map[string]any{"level": 3})

	at := func(off time.Duration) *time.Time { x := nb.Add(off); return &x }

	cases := []struct {
		name string
		at   *time.Time
		want domain.VerificationVerdict
	}{
		{"before issuance", ptrTime(t0.Add(-time.Minute)), domain.VerdictUnknown},
		{"before not_before", at(-time.Second), domain.VerdictExpired},
		{"at not_before (inclusive)", at(0), domain.VerdictValid},
		{"one nano before expiry", at(time.Hour - time.Nanosecond), domain.VerdictValid},
		{"at expiry instant (exclusive)", at(time.Hour), domain.VerdictExpired},
		{"after expiry", at(time.Hour + time.Second), domain.VerdictExpired},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res := verify(t, e.svc, c.ID, "gate-access", tc.at)
			if res.Verdict != tc.want {
				t.Fatalf("at %v: got %s (%s), want %s", tc.at, res.Verdict, res.Reason, tc.want)
			}
		})
	}
}

// TestKeyRotationIntervals verifies that rotation preserves the old key
// interval: credentials signed before rotation verify under the old key
// both at past replay times and afterwards; new credentials are signed by
// the new key.
func TestKeyRotationIntervals(t *testing.T) {
	t0 := time.Date(2026, 2, 1, 9, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("rotating-ca")

	c1 := e.issue(iss.Issuer.ID, "alice", "login", t0, t0.Add(24*time.Hour),
		map[string]any{"role": "reader"})
	if c1.Kid != iss.Keypair.Kid {
		t.Fatalf("c1 signed by %s, want genesis %s", c1.Kid, iss.Keypair.Kid)
	}

	// Advance two hours and rotate.
	e.clk.Advance(2 * time.Hour)
	rot, err := e.svc.RotateKey(e.ctx, iss.Issuer.ID, true)
	if err != nil {
		t.Fatal(err)
	}
	if rot.Key.Seq != 2 {
		t.Fatalf("new key seq=%d, want 2", rot.Key.Seq)
	}
	if rot.OldKey.RetiredAt == nil {
		t.Fatal("rotation did not close the old interval")
	}

	c2 := e.issue(iss.Issuer.ID, "bob", "login", e.clk.Now(), e.clk.Now().Add(24*time.Hour),
		map[string]any{"role": "writer"})
	if c2.Kid != rot.Key.ID {
		t.Fatalf("c2 signed by %s, want new key %s", c2.Kid, rot.Key.ID)
	}
	if c2.Kid == c1.Kid {
		t.Fatal("new credential signed by retired key")
	}

	// Replay c1 BEFORE rotation: still VALID under old key.
	before := t0.Add(time.Hour)
	if r := verify(t, e.svc, c1.ID, "login", &before); r.Verdict != domain.VerdictValid {
		t.Fatalf("c1 before rotation: %s (%s)", r.Verdict, r.Reason)
	}
	// c1 AFTER rotation but before expiry: signature still verifies
	// because key history is preserved.
	after := e.clk.Now().Add(time.Minute)
	if r := verify(t, e.svc, c1.ID, "login", &after); r.Verdict != domain.VerdictValid {
		t.Fatalf("c1 after rotation: %s (%s)", r.Verdict, r.Reason)
	}
	if r := verify(t, e.svc, c2.ID, "login", &after); r.Verdict != domain.VerdictValid {
		t.Fatalf("c2 after rotation: %s (%s)", r.Verdict, r.Reason)
	}
	// c2 cannot be validly replayed before it was issued.
	pre2 := t0.Add(90 * time.Minute)
	if r := verify(t, e.svc, c2.ID, "login", &pre2); r.Verdict != domain.VerdictUnknown {
		t.Fatalf("c2 before issuance replayed as %s, want UNKNOWN", r.Verdict)
	}
}

// TestRevocationReplayAndCache is the core "current state must not
// overwrite history" property: after revoke, the credential must verify
// VALID when replayed before the effective instant and REVOKED afterwards
// — and the post-revocation answer must carry the new snapshot and never
// come from the stale cache.
func TestRevocationReplayAndCache(t *testing.T) {
	t0 := time.Date(2026, 3, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("rev-ca")
	c := e.issue(iss.Issuer.ID, "carol", "door", t0, t0.Add(10*time.Hour),
		map[string]any{"zone": "A"})

	// First verification populates the cache; second must hit.
	r1 := verify(t, e.svc, c.ID, "door", nil)
	if r1.Verdict != domain.VerdictValid || r1.CacheHit {
		t.Fatalf("first verify: %v hit=%v", r1, r1.CacheHit)
	}
	snapValid := r1.Snapshot
	r2 := verify(t, e.svc, c.ID, "door", nil)
	if !r2.CacheHit || r2.Verdict != domain.VerdictValid || r2.Snapshot != snapValid {
		t.Fatalf("second verify should be a cache hit at same snapshot: %+v", r2)
	}

	// Revoke effective in one hour (a scheduled revocation), then move the
	// clock past its effective instant so a "now" verification sees it.
	eff := t0.Add(time.Hour)
	rev, err := e.svc.Revoke(e.ctx, service.RevokeRequest{
		CredentialID: c.ID, Reason: "key compromise suspected", EffectiveAt: &eff,
	})
	if err != nil {
		t.Fatal(err)
	}
	if rev.Snapshot <= snapValid {
		t.Fatalf("revocation snapshot %d not greater than pre-revoke snapshot %d", rev.Snapshot, snapValid)
	}

	// Immediately after recording, a now-verdict is still VALID because the
	// scheduled revocation has not taken effect — but it must already carry
	// the revocation snapshot, proving the cache saw the append.
	rScheduled := verify(t, e.svc, c.ID, "door", nil)
	if rScheduled.CacheHit {
		t.Fatal("scheduled revocation must invalidate the cache immediately")
	}
	if rScheduled.Verdict != domain.VerdictValid {
		t.Fatalf("before effective instant: %s (%s), want VALID", rScheduled.Verdict, rScheduled.Reason)
	}
	if rScheduled.Snapshot != rev.Snapshot {
		t.Fatalf("scheduled verify snapshot %d != %d", rScheduled.Snapshot, rev.Snapshot)
	}

	e.clk.Advance(time.Hour + time.Second)

	// A "now" verification after the effective instant: must NOT be served
	// from any old entry, must be REVOKED, snapshot must equal the
	// revocation snapshot.
	r3 := verify(t, e.svc, c.ID, "door", nil)
	if r3.Verdict != domain.VerdictRevoked {
		t.Fatalf("post-revocation verdict %s, want REVOKED", r3.Verdict)
	}
	if r3.Snapshot != rev.Snapshot {
		t.Fatalf("post-revocation snapshot %d != revocation snapshot %d (must be same)", r3.Snapshot, rev.Snapshot)
	}

	// Historical replay BEFORE effective_at: still VALID — history is not
	// rewritten by the current revoked state.
	before := t0.Add(30 * time.Minute)
	r4 := verify(t, e.svc, c.ID, "door", &before)
	if r4.Verdict != domain.VerdictValid {
		t.Fatalf("historical replay before revocation effective_at: %s (%s)", r4.Verdict, r4.Reason)
	}
	if r4.Snapshot != rev.Snapshot {
		t.Fatalf("historical verify should read current head %d, got %d", rev.Snapshot, r4.Snapshot)
	}

	// At the exact effective instant the revocation applies (inclusive).
	at := eff
	if r := verify(t, e.svc, c.ID, "door", &at); r.Verdict != domain.VerdictRevoked {
		t.Fatalf("at effective instant: %s, want REVOKED", r.Verdict)
	}
}

// TestBackdatedRevocation: a revocation recorded now but effective in the
// past changes historical conclusions for times after its effective_at.
func TestBackdatedRevocation(t *testing.T) {
	t0 := time.Date(2026, 4, 1, 10, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("backdate-ca")
	c := e.issue(iss.Issuer.ID, "dave", "vpn", t0, t0.Add(48*time.Hour),
		map[string]any{"group": "eng"})

	// Day 1: VALID.
	day1 := t0.Add(24 * time.Hour)
	if r := verify(t, e.svc, c.ID, "vpn", &day1); r.Verdict != domain.VerdictValid {
		t.Fatalf("day1: %s", r.Verdict)
	}

	// Day 2: discover it should have been revoked since day 1 noon.
	e.clk.Advance(48 * time.Hour)
	eff := t0.Add(24 * time.Hour)
	if _, err := e.svc.Revoke(e.ctx, service.RevokeRequest{
		CredentialID: c.ID, Reason: "late-recorded compromise", EffectiveAt: &eff,
	}); err != nil {
		t.Fatal(err)
	}

	// Day 1 01:00 (before effective): still VALID historically.
	pre := t0.Add(23 * time.Hour)
	if r := verify(t, e.svc, c.ID, "vpn", &pre); r.Verdict != domain.VerdictValid {
		t.Fatalf("before backdated effective_at: %s", r.Verdict)
	}
	// Day 1 12:00 and Day 3: REVOKED.
	if r := verify(t, e.svc, c.ID, "vpn", &day1); r.Verdict != domain.VerdictRevoked {
		t.Fatalf("after backdated effective_at: %s (%s)", r.Verdict, r.Reason)
	}
	day3 := t0.Add(72 * time.Hour)
	if r := verify(t, e.svc, c.ID, "vpn", &day3); r.Verdict != domain.VerdictRevoked {
		t.Fatalf("day3: %s", r.Verdict)
	}
}

// TestPurposeMismatch: signature valid, purpose binding rejects.
func TestPurposeMismatch(t *testing.T) {
	t0 := time.Date(2026, 5, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("purpose-ca")
	c := e.issue(iss.Issuer.ID, "erin", "email-signing", t0, t0.Add(time.Hour),
		map[string]any{"scope": "mail"})

	if r := verify(t, e.svc, c.ID, "email-signing", nil); r.Verdict != domain.VerdictValid {
		t.Fatalf("matching purpose: %s", r.Verdict)
	}
	r := verify(t, e.svc, c.ID, "server-admin", nil)
	if r.Verdict != domain.VerdictPurposeMismatch {
		t.Fatalf("mismatched purpose: %s (%s)", r.Verdict, r.Reason)
	}
}

// TestTampering is implemented as an internal test (service_test_internal.go)
// because mutating stored state requires package-private access.

// TestRetireThenIssue: after explicit retirement there is no active key,
// issuance fails until rotation happens.
func TestRetireThenIssue(t *testing.T) {
	t0 := time.Date(2026, 7, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("retire-ca")
	if _, err := e.svc.RetireKey(e.ctx, iss.Issuer.ID); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]any{"x": 1})
	_, err := e.svc.IssueCredential(e.ctx, service.IssueRequest{
		IssuerID: iss.Issuer.ID, Subject: "s", Purpose: "p",
		ExpiresAt: t0.Add(time.Hour), Content: raw,
	})
	if !errors.Is(err, domain.ErrNoActiveKey) {
		t.Fatalf("issue after retire: err=%v, want ErrNoActiveKey", err)
	}
	if _, err := e.svc.RotateKey(e.ctx, iss.Issuer.ID, true); err != nil {
		t.Fatal(err)
	}
	// The new key becomes active at t0 (rotation without clock advance).
	c := e.issue(iss.Issuer.ID, "s", "p", t0, t0.Add(time.Hour), map[string]any{"x": 1})
	if r := verify(t, e.svc, c.ID, "p", nil); r.Verdict != domain.VerdictValid {
		t.Fatalf("issue after rotate: %s", r.Verdict)
	}

	// Advancing the clock past the genesis window and rotating once more
	// demonstrates signing under a brand-new interval.
	e.clk.Advance(time.Minute)
	if _, err := e.svc.RotateKey(e.ctx, iss.Issuer.ID, true); err != nil {
		t.Fatal(err)
	}
	c2 := e.issue(iss.Issuer.ID, "s2", "p", e.clk.Now(), e.clk.Now().Add(time.Hour), map[string]any{"x": 2})
	if r := verify(t, e.svc, c2.ID, "p", nil); r.Verdict != domain.VerdictValid {
		t.Fatalf("issue after second rotate: %s", r.Verdict)
	}
	// The credential signed under the middle (already retired) key still
	// verifies historically at an instant inside its window.
	middle := t0.Add(30 * time.Second)
	if r := verify(t, e.svc, c.ID, "p", &middle); r.Verdict != domain.VerdictValid {
		t.Fatalf("historical verify under retired key: %s (%s)", r.Verdict, r.Reason)
	}
}

// TestUnknownCredentialCachesBySnapshot: an UNKNOWN verdict before issuance
// must not be served once the credential exists (head moved).
func TestUnknownCredentialSnapshotAdvance(t *testing.T) {
	t0 := time.Date(2026, 8, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("u-ca")

	missing := "cred_does_not_exist"
	r1 := verify(t, e.svc, missing, "", nil)
	if r1.Verdict != domain.VerdictUnknown {
		t.Fatalf("missing: %s", r1.Verdict)
	}
	c := e.issue(iss.Issuer.ID, "gina", "x", t0, t0.Add(time.Hour), map[string]any{"y": 2})
	r2 := verify(t, e.svc, missing, "", nil)
	// Still unknown (different id), but snapshot must have advanced past
	// the cached entry's snapshot — cache must not pin the old head.
	if r2.Snapshot <= r1.Snapshot {
		t.Fatalf("snapshot did not advance: %d -> %d", r1.Snapshot, r2.Snapshot)
	}
	if r := verify(t, e.svc, c.ID, "x", nil); r.Verdict != domain.VerdictValid {
		t.Fatalf("real credential: %s", r.Verdict)
	}
}

// TestDoubleRevokeRejected: only one revocation per credential.
func TestDoubleRevokeRejected(t *testing.T) {
	t0 := time.Date(2026, 9, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("dbl-ca")
	c := e.issue(iss.Issuer.ID, "h", "p", t0, t0.Add(time.Hour), map[string]any{"z": 3})
	if _, err := e.svc.Revoke(e.ctx, service.RevokeRequest{CredentialID: c.ID, Reason: "one"}); err != nil {
		t.Fatal(err)
	}
	_, err := e.svc.Revoke(e.ctx, service.RevokeRequest{CredentialID: c.ID, Reason: "two"})
	if !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("double revoke err=%v, want ErrConflict", err)
	}
}

// TestContentDigestHTTPRejection: content supplied to verify that hashes
// differently yields CONTENT_MISMATCH even though signature is valid.
func TestContentDigestHTTPRejection(t *testing.T) {
	t0 := time.Date(2026, 10, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("cd-ca")
	c := e.issue(iss.Issuer.ID, "i", "p", t0, t0.Add(time.Hour),
		map[string]any{"amount": 100})
	good, _ := json.Marshal(map[string]any{"amount": 100})
	bad, _ := json.Marshal(map[string]any{"amount": 999})

	r, err := e.svc.VerifyHTTP(e.ctx, service.VerifyInput{CredentialID: c.ID, Content: good})
	if err != nil || r.Verdict != domain.VerdictValid {
		t.Fatalf("good content: %v %s", err, r.Verdict)
	}
	r, err = e.svc.VerifyHTTP(e.ctx, service.VerifyInput{CredentialID: c.ID, Content: bad})
	if err != nil || r.Verdict != domain.VerdictContentMismatch {
		t.Fatalf("bad content: err=%v verdict=%s", err, r.Verdict)
	}
}

// TestSignatureCoversCanonicalPayload independently reconstructs the
// signed payload document and verifies it with the public key, proving the
// crypto is real and the envelope is self-contained.
func TestSignatureCoversCanonicalPayload(t *testing.T) {
	t0 := time.Date(2026, 11, 1, 8, 0, 0, 0, time.UTC)
	e := newEnv(t, t0)
	iss := e.issuer("sig-ca")
	c := e.issue(iss.Issuer.ID, "j", "proof", t0, t0.Add(time.Hour),
		map[string]any{"n": json.Number("100000000000000000000")})

	pub, err := crypto.DecodePublic(iss.Keypair.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	payload := mustB64(t, c.Payload)
	sig := mustB64(t, c.Signature)
	if err := crypto.Verify(pub, payload, sig); err != nil {
		t.Fatalf("enclosed payload does not verify under genesis public key: %v", err)
	}
	if !strings.Contains(string(payload), `"kid":"`+iss.Keypair.Kid+`"`) {
		t.Fatalf("payload missing kid: %s", payload)
	}
	// Large integer survived canonicalization as a number, not float-exponent.
	if !strings.Contains(string(payload), "100000000000000000000") {
		// Large integer is inside content hash, not payload; check content instead.
	}
	if !strings.Contains(string(c.Content), "100000000000000000000") {
		t.Fatalf("big int lost precision in canonical content: %s", c.Content)
	}
}

func ptrTime(t time.Time) *time.Time { return &t }

func ExampleService_roundTrip() {
	clk := clock.NewMock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	svc := service.New(memstore.New(), clk)
	iss, _ := svc.CreateIssuer(context.Background(), "demo")
	raw, _ := json.Marshal(map[string]any{"hello": "world"})
	nb := clk.Now()
	c, _ := svc.IssueCredential(context.Background(), service.IssueRequest{
		IssuerID: iss.Issuer.ID, Subject: "subject-1", Purpose: "demo",
		NotBefore: &nb, ExpiresAt: nb.Add(time.Hour), Content: raw,
	})
	r, _ := svc.VerifyCredential(context.Background(), service.VerifyRequest{
		CredentialID: c.ID, ExpectedPurpose: "demo",
	})
	fmt.Println(r.Verdict)
	// Output: VALID
}
