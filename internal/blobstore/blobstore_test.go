package blobstore

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/example/rangeserver/internal/clock"
)

func TestMemoryStoreRoundTrip(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()

	if _, err := s.Get(ctx, "nope"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("err = %v, want ErrNotFound", err)
	}
	if err := s.Put(ctx, "k", []byte("v")); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get(ctx, "k")
	if err != nil || string(got) != "v" {
		t.Fatalf("get = %q, %v", got, err)
	}

	// Defensive copy on Put.
	original := []byte("v2")
	_ = s.Put(ctx, "k2", original)
	original[0] = 'X'
	got, _ = s.Get(ctx, "k2")
	if string(got) != "v2" {
		t.Fatalf("store aliased Put input: %q", got)
	}

	// Defensive copy on Get.
	got[0] = 'Y'
	again, _ := s.Get(ctx, "k2")
	if string(again) != "v2" {
		t.Fatalf("store aliased Get output: %q", again)
	}
	if len(s.List()) != 2 {
		t.Fatalf("list = %v", s.List())
	}
}

func TestFlakyStoreFailsThenRecovers(t *testing.T) {
	inner := NewMemoryStore()
	_ = inner.Put(context.Background(), "k", []byte("data"))
	f := &FlakyStore{Inner: inner, Clock: clock.RealClock{}, Failures: 2}
	ctx := context.Background()

	for i := 1; i <= 2; i++ {
		if _, err := f.Get(ctx, "k"); !errors.Is(err, ErrUnavailable) {
			t.Fatalf("attempt %d err = %v, want ErrUnavailable", i, err)
		}
	}
	got, err := f.Get(ctx, "k")
	if err != nil {
		t.Fatalf("recovered attempt: %v", err)
	}
	if string(got) != "data" {
		t.Fatalf("got %q", got)
	}
}

func TestFlakyStoreLatencyRespectsContext(t *testing.T) {
	inner := NewMemoryStore()
	fake := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	f := &FlakyStore{Inner: inner, Clock: fake, Latency: time.Hour}

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() { _, err := f.Get(ctx, "k"); errCh <- err }()

	for fake.WaiterCount() != 1 {
		time.Sleep(time.Millisecond)
	}
	cancel()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("want context cancellation error")
		}
	case <-time.After(time.Second):
		t.Fatal("latency sleep did not respond to cancellation")
	}
}
