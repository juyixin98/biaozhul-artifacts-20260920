package sig_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"strings"
	"testing"

	"mirror-admission/internal/crypto/sig"
)

func TestCanonicalJSONStable(t *testing.T) {
	// Key order and insignificant whitespace must not change canonical bytes.
	a := []byte(`{"b":1,"a":[1,2,3],"c":{"y":true,"x":null}}`)
	b := []byte("{\n  \"c\": {\"x\": null, \"y\": true},\n  \"a\": [1, 2, 3],\n  \"b\": 1\n}")
	ca, err := sig.CanonicalJSON(a)
	if err != nil {
		t.Fatal(err)
	}
	cb, err := sig.CanonicalJSON(b)
	if err != nil {
		t.Fatal(err)
	}
	if string(ca) != string(cb) {
		t.Fatalf("canonical forms differ:\n%s\n%s", ca, cb)
	}
}

func TestCanonicalJSONRejectsTrailingData(t *testing.T) {
	if _, err := sig.CanonicalJSON([]byte(`{}{}`)); err == nil {
		t.Fatal("expected error on trailing JSON value")
	}
}

func TestSignVerifyRoundTrip(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"rule":"no_root_user","not_after":"2026-10-01T00:00:00Z"}`)
	sigB64, _, err := sig.Sign(priv, body)
	if err != nil {
		t.Fatal(err)
	}
	// Signing over the canonical form, so a re-indented copy still verifies.
	pretty := []byte("{\n  \"rule\": \"no_root_user\",\n  \"not_after\": \"2026-10-01T00:00:00Z\"\n}")
	if err := sig.Verify(pub, pretty, sigB64); err != nil {
		t.Fatalf("verify over equivalent JSON failed: %v", err)
	}
}

func TestTamperBreaksSignature(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"rule":"no_root_user"}`)
	sigB64, _, err := sig.Sign(priv, body)
	if err != nil {
		t.Fatal(err)
	}
	tampered := []byte(`{"rule":"no_privileged"}`)
	if err := sig.Verify(pub, tampered, sigB64); err == nil {
		t.Fatal("signature verified over tampered payload")
	}

	// Flip one byte of the signature itself.
	raw := decodeB64(t, sigB64)
	raw[0] ^= 0xff
	if err := sig.Verify(pub, body, encodeB64(raw)); err == nil {
		t.Fatal("flipped-signature verified")
	}

	// A different key must not verify.
	otherPub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if err := sig.Verify(otherPub, body, sigB64); err == nil {
		t.Fatal("foreign public key verified signature")
	}
}

func TestKeyIDIsStableAndDistinct(t *testing.T) {
	pub1, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	pub2, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	id1, err := sig.KeyID(pub1)
	if err != nil {
		t.Fatal(err)
	}
	id1Again, _ := sig.KeyID(pub1)
	id2, _ := sig.KeyID(pub2)
	if id1 != id1Again {
		t.Fatal("key id not deterministic")
	}
	if id1 == id2 {
		t.Fatal("distinct keys produced same id")
	}
	if !strings.HasPrefix(id1, "ed25519:") || len(id1) != len("ed25519:")+64 {
		t.Fatalf("malformed key id %q", id1)
	}
}
