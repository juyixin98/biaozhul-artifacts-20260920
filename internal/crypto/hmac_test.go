package crypto

import (
	"strings"
	"testing"
	"time"
)

func TestSignVerifyRoundTrip(t *testing.T) {
	secret := []byte("super-secret-key")
	body := []byte(`{"expected_generation":0}`)
	now := time.Now()
	h, err := Sign("k1", secret, "POST", "/api/releases/rel_x/commands/advance", now.Unix(), body)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Verify(h, "POST", "/api/releases/rel_x/commands/advance", now, body, "k1", secret); err != nil {
		t.Fatalf("verify failed: %v", err)
	}
}

func TestVerifyRejectsTamperedBody(t *testing.T) {
	secret := []byte("super-secret-key")
	body := []byte(`{"expected_generation":0}`)
	now := time.Now()
	h, _ := Sign("k1", secret, "POST", "/p", now.Unix(), body)
	tampered := []byte(`{"expected_generation":9}`)
	if _, err := Verify(h, "POST", "/p", now, tampered, "k1", secret); err == nil {
		t.Fatal("tampered body must fail verification")
	}
}

func TestVerifyRejectsWrongPathOrMethod(t *testing.T) {
	secret := []byte("s")
	now := time.Now()
	h, _ := Sign("k1", secret, "POST", "/a", now.Unix(), nil)
	if _, err := Verify(h, "POST", "/b", now, nil, "k1", secret); err == nil {
		t.Fatal("different path must fail")
	}
	if _, err := Verify(h, "PUT", "/a", now, nil, "k1", secret); err == nil {
		t.Fatal("different method must fail")
	}
}

func TestVerifyRejectsWrongKey(t *testing.T) {
	now := time.Now()
	h, _ := Sign("k1", []byte("secret-a"), "POST", "/p", now.Unix(), nil)
	if _, err := Verify(h, "POST", "/p", now, nil, "k1", []byte("secret-b")); err == nil {
		t.Fatal("wrong secret must fail")
	}
	if _, err := Verify(h, "POST", "/p", now, nil, "other-kid", []byte("secret-a")); err == nil {
		t.Fatal("wrong key id must fail")
	}
}

func TestVerifyRejectsExpiredTimestamp(t *testing.T) {
	secret := []byte("s")
	now := time.Now()
	old := now.Add(-(MaxSkew + time.Minute)).Unix()
	h, _ := Sign("k1", secret, "POST", "/p", old, nil)
	if _, err := Verify(h, "POST", "/p", now, nil, "k1", secret); err == nil ||
		!strings.Contains(err.Error(), "window") {
		t.Fatalf("old timestamp must fail with window error, got %v", err)
	}
}

func TestVerifyRejectsGarbage(t *testing.T) {
	now := time.Now()
	for _, h := range []string{"", "garbage", "kid=,ts=0,sig=", `kid=k1,ts=abc,nonce="x",sig="zz"`} {
		if _, err := Verify(h, "POST", "/p", now, nil, "k1", []byte("s")); err == nil {
			t.Fatalf("garbage header %q must fail", h)
		}
	}
}

func TestTwoSameSecondRequestsHaveDistinctSignatures(t *testing.T) {
	// Because every signature carries a fresh random nonce, two legitimate
	// identical requests in the same second must not collide (which would
	// otherwise trip replay protection).
	h1, _ := Sign("k", []byte("s"), "POST", "/p", 100, []byte("b"))
	h2, _ := Sign("k", []byte("s"), "POST", "/p", 100, []byte("b"))
	if h1 == h2 {
		t.Fatal("signatures in the same second must differ via nonce")
	}
	p1, err := ParseHeader(h1)
	if err != nil || len(p1.Nonce) != 32 {
		t.Fatalf("bad nonce: %+v err=%v", p1, err)
	}
}

func TestSignatureIsHexHMAC(t *testing.T) {
	h, _ := Sign("k", []byte("s"), "POST", "/p", 100, []byte("b"))
	p, err := ParseHeader(h)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Sig) != 64 {
		t.Fatalf("sig length=%d want 64 hex chars", len(p.Sig))
	}
	for _, c := range p.Sig {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			t.Fatalf("sig not lowercase hex: %s", p.Sig)
		}
	}
}

func TestNewIDUnique(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		id, err := NewID("rel")
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasPrefix(id, "rel_") || len(id) < 20 {
			t.Fatalf("bad id %q", id)
		}
		if seen[id] {
			t.Fatal("duplicate id")
		}
		seen[id] = true
	}
}

func TestNewNonceUnique(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		n, err := NewNonce()
		if err != nil {
			t.Fatal(err)
		}
		if len(n) != 32 || seen[n] {
			t.Fatalf("bad/dup nonce %q", n)
		}
		seen[n] = true
	}
}

func TestHashKeyDeterministic(t *testing.T) {
	if HashKey("abc") != HashKey("abc") {
		t.Fatal("hash not deterministic")
	}
	if HashKey("abc") == HashKey("abd") {
		t.Fatal("different inputs hashed equally")
	}
	if HashKey("abc") == "abc" {
		t.Fatal("hash must not be the plaintext key")
	}
}
