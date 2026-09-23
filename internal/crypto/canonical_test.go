package crypto

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestCanonicalJSONIsDeterministic(t *testing.T) {
	in := `{"b":1,"a":{"z":[1,2,3],"y":"x"},"c":null}`
	var v interface{}
	dec := json.NewDecoder(strings.NewReader(in))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		t.Fatal(err)
	}
	got, err := CanonicalJSON(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":{"y":"x","z":[1,2,3]},"b":1,"c":null}`
	if string(got) != want {
		t.Fatalf("canonical mismatch:\n got: %s\nwant: %s", got, want)
	}

	// Re-decoding and canonicalizing again must be a fixed point.
	var v2 interface{}
	dec2 := json.NewDecoder(strings.NewReader(string(got)))
	dec2.UseNumber()
	if err := dec2.Decode(&v2); err != nil {
		t.Fatal(err)
	}
	got2, err := CanonicalJSON(v2)
	if err != nil {
		t.Fatal(err)
	}
	if string(got2) != want {
		t.Fatalf("not a fixed point: %s", got2)
	}
}

func TestContentDigest(t *testing.T) {
	d1, err := ContentDigest(map[string]interface{}{"a": 1, "b": "two"})
	if err != nil {
		t.Fatal(err)
	}
	d2, err := ContentDigest(map[string]interface{}{"b": "two", "a": 1})
	if err != nil {
		t.Fatal(err)
	}
	if d1 != d2 {
		t.Fatalf("digest depends on key order: %s != %s", d1, d2)
	}
	if !strings.HasPrefix(d1, ContentDigestPrefix) {
		t.Fatalf("missing prefix: %s", d1)
	}
	ok, err := VerifyDigest(d1, map[string]interface{}{"a": 1, "b": "two"})
	if err != nil || !ok {
		t.Fatalf("digest verify failed: ok=%v err=%v", ok, err)
	}
	ok, _ = VerifyDigest(d1, map[string]interface{}{"a": 2, "b": "two"})
	if ok {
		t.Fatal("digest matched tampered content")
	}
}

func TestEd25519RoundTripAndRejection(t *testing.T) {
	pub, priv, err := GenerateEd25519Key()
	if err != nil {
		t.Fatal(err)
	}
	msg := []byte("the exact signed bytes")
	sig := Sign(priv, msg)
	if err := Verify(pub, msg, sig); err != nil {
		t.Fatalf("valid signature rejected: %v", err)
	}
	// Tampered message must fail.
	if err := Verify(pub, []byte("the exact signed byte"), sig); err == nil {
		t.Fatal("signature verified tampered message")
	}
	// Tampered signature must fail.
	bad := append([]byte(nil), sig...)
	bad[0] ^= 0xff
	if err := Verify(pub, msg, bad); err == nil {
		t.Fatal("tampered signature accepted")
	}
	// A different key must not validate the signature.
	pub2, _, err := GenerateEd25519Key()
	if err != nil {
		t.Fatal(err)
	}
	if err := Verify(pub2, msg, sig); err == nil {
		t.Fatal("signature verified under wrong key")
	}
}
