package service

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"vci/internal/clock"
	"vci/internal/crypto"
	"vci/internal/domain"
	"vci/internal/store/memstore"
)

// TestTamperedSignatureRejected flips a stored signature byte and asserts
// the REAL Ed25519 verification rejects it (rather than trusting state).
func TestTamperedSignatureRejected(t *testing.T) {
	t0 := time.Date(2026, 6, 1, 8, 0, 0, 0, time.UTC)
	clk := clock.NewMock(t0)
	st := memstore.New()
	svc := New(st, clk)
	ctx := context.Background()

	iss, err := svc.CreateIssuer(ctx, "tamper-ca")
	if err != nil {
		t.Fatal(err)
	}
	nb := t0
	raw, _ := json.Marshal(map[string]any{"doc": "hash-this"})
	c, err := svc.IssueCredential(ctx, IssueRequest{
		IssuerID: iss.Issuer.ID, Subject: "frank", Purpose: "sealing",
		NotBefore: &nb, ExpiresAt: t0.Add(time.Hour), Content: raw,
	})
	if err != nil {
		t.Fatal(err)
	}

	ok, err := svc.VerifyCredential(ctx, VerifyRequest{CredentialID: c.ID, ExpectedPurpose: "sealing"})
	if err != nil || ok.Verdict != domain.VerdictValid {
		t.Fatalf("sanity: %v %s", err, ok.Verdict)
	}

	// Tamper with the signature directly in the store, then verify — the
	// cache entry was keyed to the same snapshot, but tampering happens
	// without a snapshot append, so bypass the cache by using a fresh
	// service over the same store.
	stored, err := st.GetCredential(ctx, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	bad := append([]byte(nil), stored.Signature...)
	bad[0] ^= 0x01
	if !st.TestOnlyReplaceSignature(c.ID, bad) {
		t.Fatal("credential missing in memstore")
	}
	fresh := New(st, clk)
	r, err := fresh.VerifyCredential(ctx, VerifyRequest{CredentialID: c.ID, ExpectedPurpose: "sealing"})
	if err != nil {
		t.Fatal(err)
	}
	if r.Verdict != domain.VerdictInvalidSignature {
		t.Fatalf("tampered signature verdict=%s (%s), want INVALID_SIGNATURE", r.Verdict, r.Reason)
	}
}

// TestWrongKeySignatureRejected signs a valid-looking payload with a
// foreign key and stores it; verification must fail cryptographically.
func TestWrongKeySignatureRejected(t *testing.T) {
	t0 := time.Date(2026, 6, 2, 8, 0, 0, 0, time.UTC)
	clk := clock.NewMock(t0)
	st := memstore.New()
	svc := New(st, clk)
	ctx := context.Background()

	iss, _ := svc.CreateIssuer(ctx, "real-ca")
	_, foreignPriv, _ := crypto.GenerateEd25519Key()

	nb := t0
	raw, _ := json.Marshal(map[string]any{"doc": "x"})
	c, err := svc.IssueCredential(ctx, IssueRequest{
		IssuerID: iss.Issuer.ID, Subject: "mallory", Purpose: "sealing",
		NotBefore: &nb, ExpiresAt: t0.Add(time.Hour), Content: raw,
	})
	if err != nil {
		t.Fatal(err)
	}
	stored, _ := st.GetCredential(ctx, c.ID)
	stored.Signature = crypto.Sign(foreignPriv, stored.PayloadJSON)
	st.TestOnlyReplaceSignature(c.ID, stored.Signature)

	r, err := New(st, clk).VerifyCredential(ctx, VerifyRequest{CredentialID: c.ID, ExpectedPurpose: "sealing"})
	if err != nil {
		t.Fatal(err)
	}
	if r.Verdict != domain.VerdictInvalidSignature {
		t.Fatalf("foreign-key signature verdict=%s, want INVALID_SIGNATURE", r.Verdict)
	}
}
