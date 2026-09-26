package artifact

import (
	"errors"
	"testing"
	"time"
)

func TestNewDerivesStrongETag(t *testing.T) {
	a := New("x", []byte("abc"), "application/octet-stream", time.Now())
	if a.ETag() != Validator([]byte("abc")) {
		t.Errorf("ETag %q", a.ETag())
	}
	if a.ETag() == Validator([]byte("abd")) {
		t.Error("distinct content produced same ETag")
	}
	if a.ETag()[0] != '"' || a.ETag()[len(a.ETag())-1] != '"' {
		t.Error("ETag must be quoted")
	}
}

func TestBytesAreDefensiveCopies(t *testing.T) {
	src := []byte("immutable")
	a := New("x", src, "", time.Now())
	src[0] = 'X'
	got := a.Bytes()
	if string(got) != "immutable" {
		t.Fatalf("constructor aliased input: %q", got)
	}
	got[0] = 'Y'
	if string(a.Bytes()) != "immutable" {
		t.Fatal("Bytes() returned internal slice; caller mutated artifact")
	}
	part := a.Slice(0, 3)
	part[0] = 'Z'
	if string(a.Slice(0, 3)) != "imm" {
		t.Fatal("Slice() returned internal slice")
	}
}

func TestSlicePanicsOnBadRange(t *testing.T) {
	a := New("x", make([]byte, 4), "", time.Now())
	for _, tc := range [][2]int64{{-1, 2}, {2, 1}, {0, 5}} {
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("Slice(%d,%d) should panic", tc[0], tc[1])
				}
			}()
			_ = a.Slice(tc[0], tc[1])
		}()
	}
}

func TestStoreGetPut(t *testing.T) {
	s := NewStore().Put(New("a", []byte{1}, "", time.Now()))
	if got, err := s.Get("a"); err != nil || got.Len() != 1 {
		t.Fatalf("get: %v", err)
	}
	if _, err := s.Get("missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
	if ids := s.IDs(); len(ids) != 1 || ids[0] != "a" {
		t.Fatalf("IDs = %v", ids)
	}
}

func TestLastModifiedTruncatedToSeconds(t *testing.T) {
	in := time.Date(2026, 1, 1, 0, 0, 0, 500_000_000, time.UTC)
	a := New("x", nil, "", in)
	if !a.LastModified().Equal(in.Truncate(time.Second)) {
		t.Errorf("lastModified %v", a.LastModified())
	}
}
