package artifact

import (
	"bytes"
	"context"
	"errors"
	"testing"
	"time"
)

func TestNewCopiesBytes(t *testing.T) {
	raw := []byte("immutable")
	a := New("a", raw)
	raw[0] = 'X'
	got, err := a.Representation(RepIdentity)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "immutable" {
		t.Fatalf("artifact mutated by caller: %q", got)
	}
}

func TestRepresentationReturnsCopy(t *testing.T) {
	a := New("a", []byte("abc"))
	got, _ := a.Representation(RepIdentity)
	got[0] = 'Z'
	again, _ := a.Representation(RepIdentity)
	if again[0] != 'a' {
		t.Fatal("Representation handed out mutable state")
	}
}

func TestGzipRoundTrip(t *testing.T) {
	raw := bytes.Repeat([]byte("range-semantics-"), 64)
	a := New("g", raw)

	gz, err := a.Representation(RepGzip)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(gz, raw) {
		t.Fatal("gzip representation identical to raw")
	}
	decoded, err := Gunzip(gz)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(decoded, raw) {
		t.Fatal("gunzip did not restore canonical bytes")
	}
}

func TestDistinctValidatorsPerRepresentation(t *testing.T) {
	a := New("g", []byte("hello world"))
	idETag, err := a.ETagFor(RepIdentity)
	if err != nil {
		t.Fatal(err)
	}
	gzETag, err := a.ETagFor(RepGzip)
	if err != nil {
		t.Fatal(err)
	}
	if idETag == gzETag {
		t.Fatal("identity and gzip representations share an ETag")
	}
	if a.ETag() != idETag {
		t.Error("ETag() should equal identity validator")
	}
	if _, err := a.ETagFor("br"); !errors.Is(err, ErrUnknownEncoding) {
		t.Errorf("err = %v, want ErrUnknownEncoding", err)
	}
}

func TestLastModifiedTruncatedToSecond(t *testing.T) {
	ts := time.Date(2026, 1, 2, 3, 4, 5, 400_000_000, time.UTC)
	a := NewWithMetadata("a", nil, ts)
	if got := a.LastModified().Nanosecond(); got != 0 {
		t.Errorf("nanosecond = %d, want truncated to second", got)
	}
}

func TestContentHashStable(t *testing.T) {
	a1 := New("a", []byte("same"))
	a2 := New("b", []byte("same"))
	if a1.ContentHash() != a2.ContentHash() {
		t.Error("identical content produced different hashes")
	}
	if a1.ETag() != a2.ETag() {
		t.Error("identical content produced different ETags")
	}
}

func TestRegistry(t *testing.T) {
	r := NewRegistry()
	if _, err := r.Get(context.Background(), "missing"); !errors.Is(err, ErrUnknownArtifact) {
		t.Fatalf("err = %v, want ErrUnknownArtifact", err)
	}
	a := New("x", []byte("X"))
	r.Put(a)
	got, err := r.Get(context.Background(), "x")
	if err != nil || got != a {
		t.Fatalf("get = %v, %v", got, err)
	}
	if len(r.IDs()) != 1 {
		t.Fatalf("ids = %v", r.IDs())
	}
}
