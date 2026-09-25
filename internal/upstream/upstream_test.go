package upstream

import (
	"context"
	"testing"
	"time"

	"streamback/internal/clock"
)

func collect(ctx context.Context, s *Service, spec Spec) ([]Item, error) {
	items, errs := s.Stream(ctx, spec)
	var got []Item
	for items != nil || errs != nil {
		select {
		case it, ok := <-items:
			if !ok {
				items = nil
				continue
			}
			got = append(got, it)
		case err, ok := <-errs:
			if !ok {
				errs = nil
				continue
			}
			return got, err
		}
	}
	return got, nil
}

func TestStreamProducesAllItems(t *testing.T) {
	s := New(clock.Real{})
	got, err := collect(context.Background(), s, Spec{Count: 5, ItemBytes: 8})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 5 {
		t.Fatalf("got %d items, want 5", len(got))
	}
	for i, it := range got {
		if it.Seq != i+1 {
			t.Fatalf("item %d has seq %d", i, it.Seq)
		}
		if len(it.Payload) != 8 {
			t.Fatalf("item %d payload = %d bytes, want 8", i, len(it.Payload))
		}
	}
}

func TestStreamInjectedFailure(t *testing.T) {
	s := New(clock.Real{})
	got, err := collect(context.Background(), s, Spec{
		Count: 10, ItemBytes: 4, FailAt: 4, FailMsg: "boom",
	})
	if err == nil || err.Error() != "boom" {
		t.Fatalf("err = %v, want boom", err)
	}
	if len(got) != 3 {
		t.Fatalf("got %d items before failure, want 3", len(got))
	}
}

func TestStreamHonoursManualClock(t *testing.T) {
	mc := clock.NewManual(time.Unix(0, 0))
	s := New(mc)
	ctx := context.Background()
	items, _ := s.Stream(ctx, Spec{Count: 1, ItemBytes: 1, ItemDelay: 5000})

	select {
	case <-items:
		t.Fatal("item produced before the manual clock advanced")
	case <-time.After(50 * time.Millisecond):
	}
	if mc.Pending() != 1 {
		t.Fatalf("Pending() = %d, want 1 (producer blocked on delay)", mc.Pending())
	}

	mc.Advance(5 * time.Second)
	select {
	case it := <-items:
		if it.Seq != 1 {
			t.Fatalf("seq = %d, want 1", it.Seq)
		}
	case <-time.After(time.Second):
		t.Fatal("item not produced after advancing the clock")
	}
}

func TestStreamStopsOnCancel(t *testing.T) {
	s := New(clock.Real{})
	ctx, cancel := context.WithCancel(context.Background())
	items, errs := s.Stream(ctx, Spec{Count: 1000, ItemBytes: 1, ItemDelay: 10})

	// Read one item, then cancel; production must stop promptly.
	<-items
	cancel()
	deadline := time.After(2 * time.Second)
	for items != nil || errs != nil {
		select {
		case _, ok := <-items:
			if !ok {
				items = nil
			}
		case _, ok := <-errs:
			if !ok {
				errs = nil
			}
		case <-deadline:
			t.Fatal("channels not closed after cancel")
		}
	}
}

func TestSpecValidate(t *testing.T) {
	if err := (Spec{Count: -1}).Validate(); err == nil {
		t.Fatal("negative count accepted")
	}
	if err := (Spec{Count: 1, ItemBytes: -1}).Validate(); err == nil {
		t.Fatal("negative itemBytes accepted")
	}
	if err := (Spec{Count: 1, ItemDelay: -5}).Validate(); err == nil {
		t.Fatal("negative delay accepted")
	}
	if err := (Spec{Count: 3, ItemBytes: 1, FailAt: 2}).Validate(); err != nil {
		t.Fatalf("valid spec rejected: %v", err)
	}
}
