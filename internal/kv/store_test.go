package kv_test

import (
	"errors"
	"testing"

	"respd/internal/kv"
)

func TestSetGetEmptyVsMissing(t *testing.T) {
	s := kv.New()
	if v, ok := s.Get(0, "missing"); ok || v != nil {
		t.Fatalf("missing key must be (nil,false), got %q,%v", v, ok)
	}
	if !s.Set(0, "e", []byte(""), kv.SetOptions{}) {
		t.Fatal("set empty failed")
	}
	v, ok := s.Get(0, "e")
	if !ok || v == nil || len(v) != 0 {
		t.Fatalf("empty string must be ([]byte{},true), got %#v,%v", v, ok)
	}
}

func TestBinarySafety(t *testing.T) {
	s := kv.New()
	payload := []byte{0, 1, 2, '\r', '\n', 0xFF, 0x00}
	s.Set(0, "k", payload, kv.SetOptions{})
	got, ok := s.Get(0, "k")
	if !ok {
		t.Fatal("missing")
	}
	if string(got) != string(payload) {
		t.Fatalf("binary mismatch: %v", got)
	}
	// Mutating the caller slice must not affect storage.
	payload[0] = 0x7F
	got2, _ := s.Get(0, "k")
	if got2[0] != 0 {
		t.Fatal("store aliased caller memory")
	}
}

func TestIncrGrammar(t *testing.T) {
	s := kv.New()
	if n, err := s.Incr(0, "n", 1); err != nil || n != 1 {
		t.Fatalf("fresh incr: %d,%v", n, err)
	}
	s.Set(0, "bad", []byte(" 12"), kv.SetOptions{})
	if _, err := s.Incr(0, "bad", 1); !errors.Is(err, kv.ErrNotInteger) {
		t.Fatalf("leading space must fail, got %v", err)
	}
	s.Set(0, "plus", []byte("+1"), kv.SetOptions{})
	if _, err := s.Incr(0, "plus", 1); !errors.Is(err, kv.ErrNotInteger) {
		t.Fatalf("plus sign must fail, got %v", err)
	}
	s.Set(0, "big", []byte("9223372036854775807"), kv.SetOptions{})
	if _, err := s.Incr(0, "big", 1); !errors.Is(err, kv.ErrOverflow) {
		t.Fatalf("overflow expected, got %v", err)
	}
}

func TestSelectIsolation(t *testing.T) {
	s := kv.New()
	s.Set(0, "k", []byte("a"), kv.SetOptions{})
	s.Set(1, "k", []byte("b"), kv.SetOptions{})
	if v, _ := s.Get(0, "k"); string(v) != "a" {
		t.Fatalf("db0 = %q", v)
	}
	if v, _ := s.Get(1, "k"); string(v) != "b" {
		t.Fatalf("db1 = %q", v)
	}
	if s.DBSize(2) != 0 {
		t.Fatal("db2 should be empty")
	}
	s.FlushDB(0)
	if s.Exists(0, []string{"k"}) != 0 {
		t.Fatal("flushdb failed")
	}
}

func TestTTLExpiry(t *testing.T) {
	now := int64(1000)
	s := kv.New()
	s.SetClock(func() int64 { return now })

	s.Set(0, "t", []byte("v"), kv.SetOptions{ExpireAtMs: 5000})
	if s.Exists(0, []string{"t"}) != 1 {
		t.Fatal("key should exist at t=1000")
	}
	now = 4999
	if s.Exists(0, []string{"t"}) != 1 {
		t.Fatal("key should exist at t=4999")
	}
	now = 5000
	if s.Exists(0, []string{"t"}) != 0 {
		t.Fatal("key should have expired at t=5000")
	}

	s.Set(0, "p", []byte("v"), kv.SetOptions{})
	if ok := s.Persist(0, "p"); ok {
		t.Fatal("persist on persistent key must be false")
	}
	s.SetTTL(0, "p", 9000)
	if ok := s.Persist(0, "p"); !ok {
		t.Fatal("persist with TTL must be true")
	}
}

func TestGlob(t *testing.T) {
	s := kv.New()
	for _, k := range []string{"user:1", "user:2", "post:1", "user:10"} {
		s.Set(0, k, []byte(k), kv.SetOptions{})
	}
	check := func(pattern string, want int) {
		t.Helper()
		if got := len(s.Keys(0, pattern)); got != want {
			t.Fatalf("pattern %q: got %d keys, want %d", pattern, got, want)
		}
	}
	check("*", 4)
	check("user:*", 3)
	check("user:?", 2)
	check("post:*", 1)
	check("user:[13]*", 2) // user:1 and user:10
}
