package auth_test

import (
	"strings"
	"testing"

	"twap-service/internal/auth"
)

func TestSignVerifyRoundTrip(t *testing.T) {
	secret, err := auth.GenerateSecret()
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"symbol":"X","ts_us":1,"price":100}`)
	sig, err := auth.Sign(secret, "1700000000000000", "nonce-1", body)
	if err != nil {
		t.Fatal(err)
	}
	ok, err := auth.Verify(secret, "1700000000000000", "nonce-1", sig, body)
	if err != nil || !ok {
		t.Fatalf("valid signature rejected: ok=%v err=%v", ok, err)
	}
	// Tampered body must fail.
	ok, _ = auth.Verify(secret, "1700000000000000", "nonce-1", sig,
		[]byte(`{"symbol":"X","ts_us":1,"price":101}`))
	if ok {
		t.Fatal("tampered body accepted")
	}
	// Wrong nonce/timestamp changes the MAC.
	ok, _ = auth.Verify(secret, "1700000000000001", "nonce-1", sig, body)
	if ok {
		t.Fatal("different timestamp accepted")
	}
	ok, _ = auth.Verify(secret, "1700000000000000", "nonce-2", sig, body)
	if ok {
		t.Fatal("different nonce accepted")
	}
	// Random nonce/secret sanity.
	n1, _ := auth.GenerateNonce()
	n2, _ := auth.GenerateNonce()
	if n1 == n2 {
		t.Fatal("nonce collision")
	}
	s2, _ := auth.GenerateSecret()
	if s2 == secret {
		t.Fatal("secret collision")
	}
	if !strings.Contains(s2, "=") && len(s2) < 40 {
		t.Fatalf("secret looks too short: %s", s2)
	}
}
