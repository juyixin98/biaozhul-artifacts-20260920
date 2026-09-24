package cryptox

import (
	"bytes"
	"testing"
)

func TestSignVerify(t *testing.T) {
	c := CanonicalV1("d", 1000, 21_000, 1, map[string]string{"b": "2", "a": "1"}, "r", nil)
	sig := Sign("k", c)
	if !Verify("k", c, sig) {
		t.Fatal("valid signature failed to verify")
	}
	if Verify("wrong", c, sig) {
		t.Fatal("signature verified under wrong key")
	}
	sig2 := append([]byte(nil), sig...)
	sig2[0] ^= 1
	if Verify("k", c, sig2) {
		t.Fatal("tampered signature verified")
	}
}

func TestLabelOrderIsCanonical(t *testing.T) {
	a := CanonicalV1("d", 1, 2, 0, map[string]string{"x": "1", "y": "2"}, "r", nil)
	b := CanonicalV1("d", 1, 2, 0, map[string]string{"y": "2", "x": "1"}, "r", nil)
	if !bytes.Equal(a, b) {
		t.Fatalf("label map ordering changed canonical bytes:\n%s\n%s", a, b)
	}
}

func TestAbsentVsExplicitZero(t *testing.T) {
	zero := int64(0)
	absent := CanonicalV2("d", 1, 2, 1, nil, "r", nil)
	explicit := CanonicalV2("d", 1, 2, 1, nil, "r", &zero)
	if bytes.Equal(absent, explicit) {
		t.Fatal("absent and explicit-zero must produce different canonical bytes")
	}
	nonZero := int64(5)
	explicitNZ := CanonicalV2("d", 1, 2, 1, nil, "r", &nonZero)
	if bytes.Equal(absent, explicitNZ) || bytes.Equal(explicit, explicitNZ) {
		t.Fatal("battery value variations must be distinct")
	}
}

func TestEscapeIsInjective(t *testing.T) {
	// A pipe inside a value must not collapse to a delimiter.
	a := CanonicalV1(`x|`, 1, 2, 0, nil, "r", nil)
	b := CanonicalV1(`x`, 1, 2, 0, nil, "r", nil)
	if bytes.Equal(a, b) {
		t.Fatal("escaping failed to disambiguate embedded delimiter")
	}
}
