package digest

import (
	"bytes"
	"strings"
	"testing"
)

func TestKnownVectors(t *testing.T) {
	// SHA-256 of empty string — a fixed, independently known vector.
	if got := FromBytes(nil); got != "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Fatalf("empty digest mismatch: %s", got)
	}
	// "abc" — published NIST FIPS 180-2 test vector.
	if got := FromBytes([]byte("abc")); got != "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" {
		t.Fatalf("abc digest mismatch: %s", got)
	}
}

func TestFromReaderMatchesFromBytes(t *testing.T) {
	b := bytes.Repeat([]byte("stream-hash-"), 10000)
	d1 := FromBytes(b)
	d2, n, err := FromReader(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	if d1 != d2 {
		t.Fatalf("stream digest %s != buffer digest %s", d2, d1)
	}
	if n != int64(len(b)) {
		t.Fatalf("byte count %d != %d", n, len(b))
	}
}

func TestValid(t *testing.T) {
	good := "sha256:" + strings.Repeat("a", 64)
	if !Valid(good) {
		t.Fatal("valid digest rejected")
	}
	for _, bad := range []string{
		"", "sha256:" + strings.Repeat("A", 64), "sha256:xyz",
		"sha512:" + strings.Repeat("a", 64), "sha256:" + strings.Repeat("a", 63),
	} {
		if Valid(bad) {
			t.Fatalf("invalid digest accepted: %q", bad)
		}
		if _, err := Check(bad); err == nil {
			t.Fatalf("Check(%q) expected error", bad)
		}
	}
}
