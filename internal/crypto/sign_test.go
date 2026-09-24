package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

// TestSignVerify proves the real HMAC path accepts an untouched signed
// message and rejects tampering of every signed field.
func TestSignVerify(t *testing.T) {
	secret := "secret-dev-001"
	f := CanonicalFields{DeviceID: "dev-001", BootGen: 3, Seq: 42, Value: 213.7, TSMillis: 1727100000123}
	sig := Sign(secret, f)
	if len(sig) != 64 {
		t.Fatalf("sig length = %d, want 64 hex chars", len(sig))
	}
	if !Verify(secret, f, sig) {
		t.Fatal("Verify rejected a valid signature")
	}

	// Independently recompute the expected MAC with the standard library so the
	// test does not merely compare Sign against itself.
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(CanonicalString(f)))
	if want := hex.EncodeToString(mac.Sum(nil)); want != sig {
		t.Fatalf("sig = %s, want independently computed %s", sig, want)
	}

	tampered := []CanonicalFields{
		{DeviceID: "dev-002", BootGen: 3, Seq: 42, Value: 213.7, TSMillis: 1727100000123},
		{DeviceID: "dev-001", BootGen: 4, Seq: 42, Value: 213.7, TSMillis: 1727100000123},
		{DeviceID: "dev-001", BootGen: 3, Seq: 43, Value: 213.7, TSMillis: 1727100000123},
		{DeviceID: "dev-001", BootGen: 3, Seq: 42, Value: 213.8, TSMillis: 1727100000123},
		{DeviceID: "dev-001", BootGen: 3, Seq: 42, Value: 213.7, TSMillis: 1727100000124},
	}
	for i, g := range tampered {
		if Verify(secret, g, sig) {
			t.Fatalf("tamper case %d unexpectedly verified", i)
		}
	}
	if Verify("wrong-secret", f, sig) {
		t.Fatal("signature verified with wrong secret")
	}
	forged := sig[:63] + flip(sig[63])
	if Verify(secret, f, forged) {
		t.Fatal("forged signature verified")
	}
	if Verify(secret, f, "not-hex") {
		t.Fatal("non-hex signature verified")
	}
}

func flip(c byte) string {
	if c == '0' {
		return "1"
	}
	return "0"
}

// TestCanonicalStability fixes the exact signed byte string, guarding against
// accidental protocol changes that would invalidate real devices.
func TestCanonicalStability(t *testing.T) {
	f := CanonicalFields{DeviceID: "d", BootGen: 1, Seq: 2, Value: 213.7, TSMillis: 1000}
	want := "v1\ndevice=d\nboot=1\nseq=2\nvalue=213.7\nts=1000"
	if got := CanonicalString(f); got != want {
		t.Fatalf("canonical string = %q, want %q", got, want)
	}
}
