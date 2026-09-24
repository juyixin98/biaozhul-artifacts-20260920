package digestx

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

func TestParseValid(t *testing.T) {
	sum := sha256.Sum256([]byte("hello"))
	good := "sha256:" + hex.EncodeToString(sum[:])
	algo, enc, err := Parse(good)
	if err != nil {
		t.Fatalf("Parse(%q): %v", good, err)
	}
	if algo != "sha256" || enc != hex.EncodeToString(sum[:]) {
		t.Fatalf("unexpected parse %q %q", algo, enc)
	}
	if !Valid(good) {
		t.Fatal("Valid should be true")
	}
}

func TestParseInvalid(t *testing.T) {
	bad := []string{
		"",
		"sha256:",
		"sha512:" + strings.Repeat("a", 64),
		"sha256:ZZ" + strings.Repeat("a", 62),
		"sha256:" + strings.Repeat("A", 64), // uppercase rejected
		"sha256:" + strings.Repeat("0", 63),
		"sha256:" + strings.Repeat("0", 65),
	}
	for _, b := range bad {
		if Valid(b) {
			t.Errorf("Valid(%q) = true, want false", b)
		}
		if _, _, err := Parse(b); err == nil {
			t.Errorf("Parse(%q) err = nil, want error", b)
		}
	}
}

func TestVerifyingWriterAcceptsMatchingContent(t *testing.T) {
	content := []byte("the quick brown fox")
	want := FromBytes(content)
	var buf bytes.Buffer
	vw, err := NewVerifyingWriter(&buf, want)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := vw.Write(content); err != nil {
		t.Fatal(err)
	}
	if err := vw.Verify(); err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if !bytes.Equal(buf.Bytes(), content) {
		t.Fatal("content was not forwarded to the destination writer")
	}
	if vw.Bytes() != int64(len(content)) {
		t.Fatalf("Bytes = %d, want %d", vw.Bytes(), len(content))
	}
}

func TestVerifyingWriterRejectsTamperedContent(t *testing.T) {
	want := FromBytes([]byte("original"))
	var buf bytes.Buffer
	vw, _ := NewVerifyingWriter(&buf, want)
	_, _ = vw.Write([]byte("tampered"))
	if err := vw.Verify(); err == nil {
		t.Fatal("Verify on tampered content should fail")
	}
}

func TestHasherDigest(t *testing.T) {
	var parts = [][]byte{[]byte("abc"), []byte("def"), []byte("ghi")}
	h := NewHasher(&bytes.Buffer{})
	var cat []byte
	for _, p := range parts {
		_, _ = h.Write(p)
		cat = append(cat, p...)
	}
	if h.Digest() != FromBytes(cat) {
		t.Fatalf("streaming digest %s != whole digest %s", h.Digest(), FromBytes(cat))
	}
}
