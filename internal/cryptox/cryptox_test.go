package cryptox_test

import (
	"encoding/hex"
	"testing"
	"time"

	"sensorhealth/internal/cryptox"
)

func TestSignVerifyRoundTrip(t *testing.T) {
	secret := "the-shared-secret"
	body := []byte(`{"messages":[]}`)
	ts := time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC)

	sig := cryptox.Sign(secret, ts.Unix(), body)
	if len(sig) != 64 {
		t.Fatalf("sig length = %d, want 64 hex chars", len(sig))
	}
	if _, err := hex.DecodeString(sig); err != nil {
		t.Fatalf("signature is not hex: %v", err)
	}
	if !cryptox.Verify(secret, sig, ts.Unix(), body) {
		t.Fatal("signature must verify with the same secret, ts and body")
	}
	// Any single change breaks it.
	if cryptox.Verify("other-secret", sig, ts.Unix(), body) {
		t.Fatal("signature verified with wrong secret")
	}
	if cryptox.Verify(secret, sig, ts.Add(time.Second).Unix(), body) {
		t.Fatal("signature verified with different timestamp")
	}
	if cryptox.Verify(secret, sig, ts.Unix(), []byte(`{"messages":[1]}`)) {
		t.Fatal("signature verified over tampered body")
	}
}

// A malformed (non-hex) signature must be a clean rejection, not a panic.
func TestVerifyRejectsGarbage(t *testing.T) {
	if cryptox.Verify("s", "not-hex!!", 1, []byte("b")) {
		t.Fatal("non-hex signature must not verify")
	}
}

func TestVerifyFreshWindow(t *testing.T) {
	secret := "s"
	body := []byte("b")
	now := time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC)
	sig := cryptox.Sign(secret, now.Unix(), body)
	if err := cryptox.VerifyFresh(secret, sig, now, body, now, time.Minute); err != nil {
		t.Fatalf("fresh signature: %v", err)
	}
	old := now.Add(-2 * time.Minute)
	if err := cryptox.VerifyFresh(secret, cryptox.Sign(secret, old.Unix(), body),
		old, body, now, time.Minute); err == nil {
		t.Fatal("timestamp outside freshness window must be rejected (replay)")
	}
	if err := cryptox.VerifyFresh("", sig, now, body, now, time.Minute); err == nil {
		t.Fatal("empty server secret must fail honestly")
	}
}

func TestNewSecretIsRandomAndFullEntropy(t *testing.T) {
	a, err := cryptox.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	b, err := cryptox.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	if a == b {
		t.Fatal("two generated secrets must differ (crypto/rand)")
	}
	if len(a) != 64 {
		t.Fatalf("secret length = %d, want 64 hex chars (32 bytes)", len(a))
	}
}
