package canceltree

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// blockingLeaf blocks on ctx until canceled, recording when it observed
// cancellation and whether cleanup ran.
type harness struct {
	started    atomic.Int64
	canceled   atomic.Int64
	cleanups   atomic.Int64
	barrier    chan struct{} // closed once all leaves have started
	notifyOnce sync.Once
}

func newHarness(n int) *harness {
	return &harness{barrier: make(chan struct{}, n)}
}

func (h *harness) markStart() {
	h.started.Add(1)
	h.barrier <- struct{}{}
}

func (h *harness) waitStarted(t *testing.T, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for h.started.Load() != int64(n) && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if h.started.Load() != int64(n) {
		t.Fatalf("only %d/%d leaves started", h.started.Load(), n)
	}
}

func blockingLeaf(h *harness, name string, fatal bool) Leaf {
	return Leaf{
		Name:  name,
		Fatal: fatal,
		Run: func(ctx context.Context) error {
			h.markStart()
			<-ctx.Done()
			h.canceled.Add(1)
			return ctx.Err()
		},
		Cleanup: func(context.Context) error {
			h.cleanups.Add(1)
			return nil
		},
	}
}

func TestTree_AllSucceed(t *testing.T) {
	h := newHarness(3)
	leaves := []Leaf{
		blockingLeaf(h, "a", true),
		blockingLeaf(h, "b", false),
		blockingLeaf(h, "c", true),
	}
	// Replace blocking bodies with immediate-success bodies but keep counts.
	for i := range leaves {
		leaves[i].Run = func(context.Context) error { h.markStart(); return nil }
	}
	rep := New(leaves...).Run(context.Background())
	if !rep.OK {
		t.Fatalf("OK=false, want true: %+v", rep)
	}
	if rep.FatalTrigger != "" {
		t.Fatalf("unexpected fatal trigger %q", rep.FatalTrigger)
	}
	for _, l := range rep.Leaves {
		if l.Outcome != OutcomeSucceeded {
			t.Fatalf("leaf %s outcome %s, want succeeded", l.Name, l.Outcome)
		}
	}
}

func TestTree_FirstFatalCancelsOthersAndAwaitsCleanup(t *testing.T) {
	h := newHarness(3)
	fatalErr := errors.New("boom")
	leaves := []Leaf{
		blockingLeaf(h, "slow-a", true),
		blockingLeaf(h, "slow-b", false),
		{
			Name:  "fatal-c",
			Fatal: true,
			Run: func(ctx context.Context) error {
				h.markStart()
				return fatalErr
			},
			Cleanup: func(context.Context) error { h.cleanups.Add(1); return nil },
		},
	}
	rep := New(leaves...).Run(context.Background())

	if rep.OK {
		t.Fatal("OK=true, want false after fatal leaf")
	}
	if rep.FatalTrigger != "fatal-c" {
		t.Fatalf("fatal trigger = %q, want fatal-c", rep.FatalTrigger)
	}
	if got := h.canceled.Load(); got != 2 {
		t.Fatalf("canceled leaves = %d, want 2", got)
	}
	// Run must not return until ALL cleanups finished.
	if got := h.cleanups.Load(); got != 3 {
		t.Fatalf("cleanups run = %d, want 3 (Run must await cleanup)", got)
	}
	outcomes := map[string]Outcome{}
	for _, l := range rep.Leaves {
		outcomes[l.Name] = l.Outcome
		if l.Cleanup != CleanupRan {
			t.Fatalf("leaf %s cleanup = %s, want ran", l.Name, l.Cleanup)
		}
	}
	if outcomes["slow-a"] != OutcomeCanceled || outcomes["slow-b"] != OutcomeCanceled {
		t.Fatalf("siblings not marked canceled: %+v", outcomes)
	}
	if outcomes["fatal-c"] != OutcomeFailed {
		t.Fatalf("fatal-c outcome = %s, want failed", outcomes["fatal-c"])
	}
}

func TestTree_NonFatalFailureDoesNotCancel(t *testing.T) {
	h := newHarness(2)
	releaseSlow := make(chan struct{})
	leaves := []Leaf{
		{
			Name:  "besteffort",
			Fatal: false,
			Run: func(context.Context) error {
				h.markStart()
				return errors.New("transient")
			},
		},
		{
			Name:  "slow",
			Fatal: true,
			Run: func(ctx context.Context) error {
				h.markStart()
				select {
				case <-releaseSlow:
					return nil
				case <-ctx.Done():
					h.canceled.Add(1)
					return ctx.Err()
				}
			},
		},
	}
	done := make(chan Report, 1)
	go func() { done <- New(leaves...).Run(context.Background()) }()
	h.waitStarted(t, 2)
	// The best-effort error must NOT have canceled the slow leaf.
	select {
	case <-done:
		t.Fatal("Run returned while slow leaf still running — non-fatal error canceled the tree")
	case <-time.After(50 * time.Millisecond):
	}
	close(releaseSlow)
	rep := <-done
	if !rep.OK {
		t.Fatalf("OK=false, best-effort failure must be tolerated: %+v", rep)
	}
	for _, l := range rep.Leaves {
		if l.Name == "besteffort" && l.Outcome != OutcomeFailed {
			t.Fatalf("besteffort outcome %s, want failed (recorded)", l.Outcome)
		}
	}
}

func TestTree_RootCancelCancelsAll(t *testing.T) {
	h := newHarness(3)
	leaves := []Leaf{
		blockingLeaf(h, "a", false),
		blockingLeaf(h, "b", true),
		blockingLeaf(h, "c", false),
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Report, 1)
	go func() { done <- New(leaves...).Run(ctx) }()
	h.waitStarted(t, 3)
	cancel() // simulate client disconnect
	rep := <-done
	if rep.OK || !rep.Canceled {
		t.Fatalf("OK=%v Canceled=%v, want false/true", rep.OK, rep.Canceled)
	}
	if rep.FatalTrigger != "" {
		t.Fatalf("fatal trigger %q on root cancel, want none", rep.FatalTrigger)
	}
	if got := h.canceled.Load(); got != 3 {
		t.Fatalf("canceled leaves = %d, want 3", got)
	}
	if got := h.cleanups.Load(); got != 3 {
		t.Fatalf("cleanups = %d, want 3", got)
	}
}

func TestTree_CleanupRunsUnderCanceledContextAndFailureIsRecorded(t *testing.T) {
	h := newHarness(1)
	cleanupErr := errors.New("cleanup failed")
	tree := New(Leaf{
		Name:  "a",
		Fatal: true,
		Run: func(ctx context.Context) error {
			h.markStart()
			<-ctx.Done()
			return ctx.Err()
		},
		Cleanup: func(ctx context.Context) error {
			h.cleanups.Add(1)
			if ctx.Err() == nil {
				t.Error("cleanup received live context, want canceled tree context")
			}
			return cleanupErr
		},
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Report, 1)
	go func() { done <- tree.Run(ctx) }()
	h.waitStarted(t, 1)
	cancel()
	rep := <-done
	lr := rep.Leaves[0]
	if lr.Cleanup != CleanupFailed || !strings.Contains(lr.CleanupError, "cleanup failed") {
		t.Fatalf("cleanup outcome=%s err=%q, want failed with message", lr.Cleanup, lr.CleanupError)
	}
}

func TestTree_PanicLeafFailsAndIsFatal(t *testing.T) {
	h := newHarness(2)
	tree := New(
		blockingLeaf(h, "slow", false),
		Leaf{
			Name: "panicker",
			Run: func(context.Context) error {
				h.markStart()
				panic("kaboom")
			},
		},
	)
	rep := tree.Run(context.Background())
	if rep.FatalTrigger != "panicker" {
		t.Fatalf("trigger = %q, want panicker", rep.FatalTrigger)
	}
	for _, l := range rep.Leaves {
		if l.Name == "panicker" && l.Outcome != OutcomePanicked {
			t.Fatalf("panicker outcome = %s", l.Outcome)
		}
	}
	if h.canceled.Load() != 1 {
		t.Fatalf("sibling canceled = %d, want 1", h.canceled.Load())
	}
}

func TestTree_SecondFatalAfterFirstIsNotTrigger(t *testing.T) {
	h := newHarness(2)
	gate := make(chan struct{})
	tree := New(
		Leaf{
			Name:  "first",
			Fatal: true,
			Run: func(context.Context) error {
				h.markStart()
				return errors.New("first error")
			},
		},
		Leaf{
			Name:  "second",
			Fatal: true,
			Run: func(ctx context.Context) error {
				h.markStart()
				select {
				case <-gate:
				case <-ctx.Done():
					return ctx.Err()
				}
				if ctx.Err() != nil {
					return ctx.Err()
				}
				return errors.New("second error")
			},
		},
	)
	rep := tree.Run(context.Background())
	if rep.FatalTrigger != "first" {
		t.Fatalf("trigger = %q, want first", rep.FatalTrigger)
	}
}

func TestTree_EmptyTreeOK(t *testing.T) {
	rep := New().Run(context.Background())
	if !rep.OK {
		t.Fatalf("empty tree OK=false: %+v", rep)
	}
}
