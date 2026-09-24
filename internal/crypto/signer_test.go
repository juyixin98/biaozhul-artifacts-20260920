package crypto

import (
	"strconv"
	"testing"
	"time"
)

func TestSignVerify_RoundTrip(t *testing.T) {
	secret := []byte("super-secret-key")
	s := NewSigner(secret)
	now := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	body := []byte(`{"device_id":"d1"}`)
	ts := now
	sig := Sign(secret, "POST", "/api/v1/ingest", ts, body)
	if err := s.Verify("POST", "/api/v1/ingest", ts, "nonce-1", sig, body, now); err != nil {
		t.Fatalf("valid signature rejected: %v", err)
	}
}

func TestVerify_TamperedBody(t *testing.T) {
	secret := []byte("k")
	s := NewSigner(secret)
	now := time.Now()
	body := []byte(`{"a":1}`)
	sig := Sign(secret, "POST", "/x", now, body)
	tampered := []byte(`{"a":2}`)
	if err := s.Verify("POST", "/x", now, "n", sig, tampered, now); err != ErrBadSignature {
		t.Fatalf("expected ErrBadSignature, got %v", err)
	}
}

func TestVerify_WrongSecret(t *testing.T) {
	now := time.Now()
	body := []byte("b")
	sig := Sign([]byte("right"), "POST", "/x", now, body)
	s := NewSigner([]byte("wrong"))
	if err := s.Verify("POST", "/x", now, "n", sig, body, now); err != ErrBadSignature {
		t.Fatalf("expected ErrBadSignature, got %v", err)
	}
}

func TestVerify_TimestampSkew(t *testing.T) {
	secret := []byte("k")
	s := NewSigner(secret)
	now := time.Now()
	old := now.Add(-10 * time.Minute)
	body := []byte("b")
	sig := Sign(secret, "POST", "/x", old, body)
	if err := s.Verify("POST", "/x", old, "n", sig, body, now); err != ErrTimestampSkew {
		t.Fatalf("expected ErrTimestampSkew, got %v", err)
	}
}

func TestVerify_Replay(t *testing.T) {
	secret := []byte("k")
	s := NewSigner(secret)
	now := time.Now()
	body := []byte("b")
	sig := Sign(secret, "POST", "/x", now, body)
	if err := s.Verify("POST", "/x", now, "dup-nonce", sig, body, now); err != nil {
		t.Fatalf("first accept: %v", err)
	}
	if err := s.Verify("POST", "/x", now, "dup-nonce", sig, body, now); err != ErrReplay {
		t.Fatalf("expected ErrReplay, got %v", err)
	}
}

func TestVerify_DifferentNoncesSameBody(t *testing.T) {
	secret := []byte("k")
	s := NewSigner(secret)
	now := time.Now()
	body := []byte("b")
	sig := Sign(secret, "POST", "/x", now, body)
	for i := 0; i < 3; i++ {
		if err := s.Verify("POST", "/x", now, "n"+strconv.Itoa(i), sig, body, now); err != nil {
			t.Fatalf("accept %d: %v", i, err)
		}
	}
}

func TestCanonicalJSON_SortsKeys(t *testing.T) {
	out, err := CanonicalJSON([]byte(`{"b":1,"a":[2,1],"c":{"z":1,"y":2}}`))
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":[2,1],"b":1,"c":{"y":2,"z":1}}`
	if string(out) != want {
		t.Fatalf("canonical mismatch:\n got %s\nwant %s", out, want)
	}
}
