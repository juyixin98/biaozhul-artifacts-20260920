package idem

import (
	"strings"
	"testing"
)

func TestFingerprint_BindsMethodPathAndBody(t *testing.T) {
	body := []byte(`{"amount":100,"account":"a"}`)
	a := Fingerprint("POST", "/v1/deposits", body)
	b := Fingerprint("POST", "/v1/deposits", body)
	if a != b {
		t.Fatalf("same request must hash identically: %q != %q", a, b)
	}
	if got := Fingerprint("GET", "/v1/deposits", body); got == a {
		t.Fatal("method must participate in fingerprint")
	}
	if got := Fingerprint("POST", "/v1/other", body); got == a {
		t.Fatal("path must participate in fingerprint")
	}
	if got := Fingerprint("POST", "/v1/deposits", []byte(`{"amount":200,"account":"a"}`)); got == a {
		t.Fatal("body must participate in fingerprint")
	}
}

func TestFingerprint_JsonKeyOrderIsCanonicalized(t *testing.T) {
	one := []byte(`{"account":"a","amount":100}`)
	two := []byte(`{"amount":100,"account":"a"}`)
	if Fingerprint("POST", "/p", one) != Fingerprint("POST", "/p", two) {
		t.Fatal("JSON objects differing only in key order must have equal fingerprints")
	}
	// 空白差异同样忽略
	if Fingerprint("POST", "/p", []byte(`{ "account": "a", "amount": 100 }`)) != Fingerprint("POST", "/p", one) {
		t.Fatal("insignificant whitespace must not change fingerprint")
	}
}

func TestFingerprint_NonJsonIsByteBound(t *testing.T) {
	a := Fingerprint("POST", "/p", []byte(`raw-text`))
	b := Fingerprint("POST", "/p", []byte(`raw-textx`))
	if a == b {
		t.Fatal("non-JSON bodies must be bound byte-for-byte")
	}
	if len(strings.TrimSpace(a)) != 64 {
		t.Fatalf("fingerprint must be 32-byte hex, got len=%d", len(a))
	}
}

func TestBodyDigest(t *testing.T) {
	if BodyDigest(nil) == BodyDigest([]byte(`x`)) {
		t.Fatal("empty and non-empty bodies should differ")
	}
	if BodyDigest([]byte(`{"a":1,"b":2}`)) != BodyDigest([]byte(`{"b":2,"a":1}`)) {
		t.Fatal("digest should canonicalize JSON key order")
	}
}
