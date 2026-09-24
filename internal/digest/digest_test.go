package digest

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
)

func TestParseAndRoundTrip(t *testing.T) {
	in := "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	d, err := Parse(in)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if d.Algorithm() != "sha256" || d.Encoded() != in[strings.IndexByte(in, ':')+1:] {
		t.Fatalf("unexpected parsed digest: %#v", d)
	}
	if d.String() != in {
		t.Fatalf("round trip = %q, want %q", d.String(), in)
	}
}

func TestParseRejects(t *testing.T) {
	bad := []string{
		"",
		"noseparator",
		":0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		"sha256:",
		"md5:0123456789abcdef0123456789abcdef", // unsupported algorithm
		"sha256:ZZZ",                           // not hex / too short
		"sha256:0123",                          // too short
	}
	for _, in := range bad {
		if _, err := Parse(in); err == nil {
			t.Errorf("Parse(%q) unexpectedly succeeded", in)
		} else if !errors.Is(err, ErrInvalid) {
			t.Errorf("Parse(%q) error %v does not wrap ErrInvalid", in, err)
		}
	}
}

// TestVerifyReaderUsesRealSHA256 computes a hash independently with the
// standard library and confirms verification accepts it.
func TestVerifyReaderUsesRealSHA256(t *testing.T) {
	content := []byte("the quick brown fox jumps over the lazy dog")
	got, n, err := FromReader("sha256", bytes.NewReader(content))
	if err != nil {
		t.Fatal(err)
	}
	if n != int64(len(content)) {
		t.Fatalf("size = %d, want %d", n, len(content))
	}
	// Independent reference computed straight from the standard library.
	sum := sha256.Sum256(content)
	want := "sha256:" + hex.EncodeToString(sum[:])
	if got.String() != want {
		t.Fatalf("digest = %s, want independently computed %s", got, want)
	}
	if err := func() error {
		_, verr := VerifyReader(got, int64(len(content)), bytes.NewReader(content))
		return verr
	}(); err != nil {
		t.Fatalf("verification of correct content failed: %v", err)
	}

	// Tamper one byte: digest verification must fail.
	tampered := append([]byte(nil), content...)
	tampered[0]++
	if _, err := VerifyReader(got, int64(len(tampered)), bytes.NewReader(tampered)); !errors.Is(err, ErrMismatch) {
		t.Fatalf("tampered content: error %v, want ErrMismatch", err)
	}

	// Wrong declared size with matching stream length reporting must surface
	// as a size mismatch (construct by claiming a different size on correct bytes).
	if _, err := VerifyReader(got, int64(len(content))+1, bytes.NewReader(content)); !errors.Is(err, ErrSizeMismatch) {
		t.Fatalf("wrong size: error %v, want ErrSizeMismatch", err)
	}
}

func TestSHA512Supported(t *testing.T) {
	content := []byte("abc")
	d, _, err := FromReader("sha512", bytes.NewReader(content))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(d.String(), "sha512:") || len(d.Encoded()) != 128 {
		t.Fatalf("unexpected sha512 digest: %s", d)
	}
}
