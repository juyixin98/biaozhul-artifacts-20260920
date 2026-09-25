package batchagg

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeExec groups records every batch it saw; its behavior per item is driven
// by the payload text so tests can request individual failures.
type fakeExec struct {
	mu      sync.Mutex
	batches [][]Item
	calls   int
	delay   time.Duration
	// wholeBatchErr, when non-nil, fails every item of every batch.
	wholeBatchErr error
}

func (f *fakeExec) Execute(ctx context.Context, batchID string, items []Item) ([]*ItemResult, error) {
	f.mu.Lock()
	f.calls++
	snapshot := make([]Item, len(items))
	copy(snapshot, items)
	f.batches = append(f.batches, snapshot)
	delay := f.delay
	werr := f.wholeBatchErr
	f.mu.Unlock()

	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if werr != nil {
		return nil, werr
	}
	results := make([]*ItemResult, len(items))
	for i, it := range items {
		if strings.HasPrefix(string(it.Payload), "FAIL:") {
			results[i] = &ItemResult{
				Index:   i,
				BatchID: batchID,
				Err:     fmt.Errorf("forced failure for %s", it.ID),
			}
			continue
		}
		results[i] = &ItemResult{
			Index:   i,
			BatchID: batchID,
			Output:  append([]byte("ok:"), it.Payload...),
		}
	}
	return results, nil
}

func (f *fakeExec) callCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func (f *fakeExec) batchSizes() []int {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]int, len(f.batches))
	for i, b := range f.batches {
		out[i] = len(b)
	}
	return out
}

func newTestScheduler(t *testing.T, cfg Config, exec Executor) (*Scheduler, *VirtualClock, *EventRecorder) {
	t.Helper()
	vc := NewVirtualClockAt(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	rec := NewEventRecorder()
	if cfg.Clock == nil {
		cfg.Clock = vc
	}
	cfg.Sink = rec
	s := New(cfg, exec)
	t.Cleanup(func() { _ = s.Close() })
	return s, vc, rec
}

func mustGet(t *testing.T, fut *Future) *ItemResult {
	t.Helper()
	res, err := fut.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected Get error: %v", err)
	}
	if res == nil {
		t.Fatal("settled future returned nil result")
	}
	return res
}

// TestLowTraffic_WaitFlush is acceptance case 1: few requests never fill a
// batch, so the MaxWait timer alone must dispatch it.
func TestLowTraffic_WaitFlush(t *testing.T) {
	exec := &fakeExec{}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 10, MaxBytes: 1000, MaxWait: 20 * time.Millisecond}, exec)

	futs := submitN(t, s, "model-a", 3, 10)

	// Wait until the runner has enqueued all items and started its wait timer
	// before advancing the virtual clock (the runner is a goroutine).
	waitForEventCount(t, rec, EventSubmitted, 3, time.Second)
	if exec.callCount() != 0 {
		t.Fatalf("batch executed before wait deadline: calls=%d", exec.callCount())
	}

	vc.Advance(19 * time.Millisecond)
	if exec.callCount() != 0 {
		t.Fatalf("batch executed 1ms early")
	}
	vc.Advance(1 * time.Millisecond)
	waitForResults(t, rec, 3, time.Second)
	if got, want := exec.callCount(), 1; got != want {
		t.Fatalf("executor calls: got %d want %d", got, want)
	}
	if sizes := exec.batchSizes(); len(sizes) != 1 || sizes[0] != 3 {
		t.Fatalf("batch sizes: %v, want [3]", sizes)
	}

	// Each request gets its own independent result, in request order.
	for i, fut := range futs {
		res := mustGet(t, fut)
		if res.Err != nil {
			t.Fatalf("item %d: unexpected err %v", i, res.Err)
		}
		want := "ok:" + fmt.Sprintf("%s-%d-%s", "model-a", i, strings.Repeat("p", 10))
		if string(res.Output) != want {
			t.Fatalf("item %d output: got %q want %q", i, res.Output, want)
		}
		if res.Index != i {
			t.Fatalf("item %d index: got %d", i, res.Index)
		}
		if res.BatchID == "" {
			t.Fatalf("item %d has no batch id", i)
		}
	}

	flushes := rec.OfType(EventBatchFlush)
	if len(flushes) != 1 || flushes[0].Reason != "wait" {
		t.Fatalf("flush events: %+v, want one wait flush", flushes)
	}
	if opens := rec.OfType(EventBatchOpen); len(opens) != 1 {
		t.Fatalf("open events: %d, want 1", len(opens))
	}
}

// TestHighTraffic_ItemsFlush is acceptance case 2: enough items to fill a
// batch dispatches immediately, without advancing the clock, then later low
// traffic waits for the timer.
func TestHighTraffic_ItemsFlush(t *testing.T) {
	exec := &fakeExec{}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 4, MaxBytes: 10_000, MaxWait: 50 * time.Millisecond}, exec)

	full := submitN(t, s, "model-b", 4, 10)
	waitForResults(t, rec, 4, time.Second)
	if got, want := exec.callCount(), 1; got != want {
		t.Fatalf("full batch dispatch: calls got %d want %d", got, want)
	}
	for i, fut := range full {
		res := mustGet(t, fut)
		if res.Err != nil || res.Index != i {
			t.Fatalf("full-batch item %d: %+v err=%v", i, res, res.Err)
		}
	}

	tail := submitN(t, s, "model-b", 2, 10)
	waitForEventCount(t, rec, EventSubmitted, 6, time.Second)
	if got := exec.callCount(); got != 1 {
		t.Fatalf("tail dispatched early: calls=%d", got)
	}
	vc.Advance(50 * time.Millisecond)
	waitForResults(t, rec, 6, time.Second)
	if got, want := exec.callCount(), 2; got != want {
		t.Fatalf("tail dispatch after wait: calls got %d want %d", got, want)
	}
	if sizes := exec.batchSizes(); len(sizes) != 2 || sizes[0] != 4 || sizes[1] != 2 {
		t.Fatalf("batch sizes: %v, want [4 2]", sizes)
	}
	for i, fut := range tail {
		if res := mustGet(t, fut); res.Err != nil {
			t.Fatalf("tail item %d: %v", i, res.Err)
		}
	}

	// The immediate flush must be reason "items"; the tail is reason "wait".
	var reasons []string
	for _, e := range rec.OfType(EventBatchFlush) {
		reasons = append(reasons, e.Reason)
	}
	if len(reasons) != 2 || reasons[0] != "items" || reasons[1] != "wait" {
		t.Fatalf("flush reasons: %v, want [items wait]", reasons)
	}
}

// TestBytesLimit_FlushesAndSplits verifies the byte constraint: a batch that
// reaches MaxBytes flushes, and a following item starts a new batch instead
// of being crammed over the limit.
func TestBytesLimit_FlushesAndSplits(t *testing.T) {
	exec := &fakeExec{}
	// 3 items x 40 bytes = 120 >= 100; MaxItems is far above so bytes drive.
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 100, MaxBytes: 100, MaxWait: time.Hour}, exec)

	payload := strings.Repeat("x", 40)
	var futs []*Future
	for i := 0; i < 3; i++ {
		fut, err := s.Submit(context.Background(), Item{
			ID:      fmt.Sprintf("byte-%d", i),
			Key:     "model-c",
			Payload: []byte(payload),
		})
		if err != nil {
			t.Fatal(err)
		}
		futs = append(futs, fut)
	}
	waitForResults(t, rec, 3, time.Second)
	if got, want := exec.callCount(), 1; got != want {
		t.Fatalf("byte flush calls: got %d want %d", got, want)
	}
	if sizes := exec.batchSizes(); sizes[0] != 3 {
		t.Fatalf("first batch sizes: %v want [3]", sizes)
	}
	for _, fut := range futs {
		if res := mustGet(t, fut); res.Err != nil {
			t.Fatal(res.Err)
		}
	}

	// Another item opens a fresh batch that waits on the timer.
	fut, err := s.Submit(context.Background(), Item{ID: "byte-3", Key: "model-c", Payload: []byte(payload)})
	if err != nil {
		t.Fatal(err)
	}
	waitForEventCount(t, rec, EventSubmitted, 4, time.Second)
	if got := exec.callCount(); got != 1 {
		t.Fatalf("new item flushed early: calls=%d", got)
	}
	vc.Advance(time.Hour)
	waitForResults(t, rec, 4, time.Second)
	if got := exec.callCount(); got != 2 {
		t.Fatalf("second batch after wait: calls=%d", got)
	}
	if res := mustGet(t, fut); res.Err != nil {
		t.Fatal(res.Err)
	}
}

// TestOversizeItem_Rejected is the explicit oversize-refusal requirement: an
// item larger than MaxBytes is refused at Submit and never reaches the
// executor, while a normal item still works.
func TestOversizeItem_Rejected(t *testing.T) {
	exec := &fakeExec{}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 10, MaxBytes: 100, MaxWait: 50 * time.Millisecond}, exec)

	_, err := s.Submit(context.Background(), Item{
		ID:      "huge",
		Key:     "model-d",
		Payload: []byte(strings.Repeat("z", 101)),
	})
	if !errors.Is(err, ErrOversizeItem) {
		t.Fatalf("oversize submit: got %v, want ErrOversizeItem", err)
	}

	ok, err := s.Submit(context.Background(), Item{ID: "small", Key: "model-d", Payload: []byte("hi")})
	if err != nil {
		t.Fatalf("normal submit after oversize: %v", err)
	}
	waitForEventCount(t, rec, EventSubmitted, 1, time.Second)
	if exec.callCount() != 0 {
		t.Fatal("executor must not see rejected items")
	}
	vc.Advance(50 * time.Millisecond)
	waitForResults(t, rec, 1, time.Second)
	res := mustGet(t, ok)
	if string(res.Output) != "ok:hi" {
		t.Fatalf("normal item output after rejection: %q", res.Output)
	}
	if exec.callCount() != 1 || len(exec.batches[0]) != 1 {
		t.Fatalf("executor batches: %v, want exactly one single-item batch", exec.batchSizes())
	}

	var sawReject bool
	for _, e := range rec.OfType(EventRejected) {
		if e.ItemID == "huge" && e.Reason == "oversize" && e.Bytes == 101 && e.MaxBytes == 100 {
			sawReject = true
		}
	}
	if !sawReject {
		t.Fatal("missing structured oversize rejection event")
	}
}

// TestPartialBatchFailure is acceptance case 3: one failed item in a batch
// does not contaminate its siblings; each request reads its own result.
func TestPartialBatchFailure(t *testing.T) {
	exec := &fakeExec{}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 5, MaxBytes: 10_000, MaxWait: 50 * time.Millisecond}, exec)

	mk := func(id, p string) *Future {
		fut, err := s.Submit(context.Background(), Item{ID: id, Key: "model-e", Payload: []byte(p)})
		if err != nil {
			t.Fatal(err)
		}
		return fut
	}
	f1 := mk("p-0", "good-0")
	f2 := mk("p-1", "FAIL:boom")
	f3 := mk("p-2", "good-2")

	waitForEventCount(t, rec, EventSubmitted, 3, time.Second)
	vc.Advance(50 * time.Millisecond)
	waitForResults(t, rec, 3, time.Second)
	if got := exec.callCount(); got != 1 {
		t.Fatalf("calls=%d want 1", got)
	}

	r1 := mustGet(t, f1)
	r2 := mustGet(t, f2)
	r3 := mustGet(t, f3)

	if r1.Err != nil || string(r1.Output) != "ok:good-0" {
		t.Fatalf("item0 should succeed: out=%q err=%v", r1.Output, r1.Err)
	}
	if r2.Err == nil || !strings.Contains(r2.Err.Error(), "forced failure for p-1") {
		t.Fatalf("item1 should fail independently, got out=%q err=%v", r2.Output, r2.Err)
	}
	if r3.Err != nil || string(r3.Output) != "ok:good-2" {
		t.Fatalf("item2 should succeed despite sibling failure: out=%q err=%v", r3.Output, r3.Err)
	}
	if r1.BatchID != r2.BatchID || r2.BatchID != r3.BatchID {
		t.Fatalf("all three should share one batch: %q %q %q", r1.BatchID, r2.BatchID, r3.BatchID)
	}
}

// TestWholeBatchErrorFailsEachItem checks the executor-level failure path:
// every item gets the same error, delivered independently.
func TestWholeBatchErrorFailsEachItem(t *testing.T) {
	exec := &fakeExec{wholeBatchErr: errors.New("backend down")}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 10, MaxBytes: 10_000, MaxWait: 10 * time.Millisecond}, exec)

	futs := submitN(t, s, "model-f", 3, 5)
	waitForEventCount(t, rec, EventSubmitted, 3, time.Second)
	vc.Advance(10 * time.Millisecond)
	waitForResults(t, rec, 3, time.Second)
	for i, fut := range futs {
		res, err := fut.Get(context.Background())
		if err != nil {
			t.Fatalf("item %d Get: %v", i, err)
		}
		if res == nil {
			t.Fatalf("item %d: missing independent result", i)
		}
		if res.Err == nil || res.Err.Error() != "backend down" {
			t.Fatalf("item %d: got err=%v want backend down", i, res.Err)
		}
	}
}

// TestCancellation_AffectsOnlyOwnItem covers "cancel only affects the
// corresponding item": cancel one queued item; the rest of the batch flushes
// normally and all siblings get independent results; the canceled item gets
// context.Canceled.
func TestCancellation_AffectsOnlyOwnItem(t *testing.T) {
	exec := &fakeExec{}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 10, MaxBytes: 10_000, MaxWait: 100 * time.Millisecond}, exec)

	keepCtx := context.Background()
	f1, err := s.Submit(keepCtx, Item{ID: "keep-1", Key: "model-g", Payload: []byte("one")})
	if err != nil {
		t.Fatal(err)
	}

	cancelCtx, cancel := context.WithCancel(context.Background())
	f2, err := s.Submit(cancelCtx, Item{ID: "drop", Key: "model-g", Payload: []byte("two")})
	if err != nil {
		t.Fatal(err)
	}

	f3, err := s.Submit(keepCtx, Item{ID: "keep-2", Key: "model-g", Payload: []byte("three")})
	if err != nil {
		t.Fatal(err)
	}

	// Make sure all three items are enqueued before canceling one.
	waitForEventCount(t, rec, EventSubmitted, 3, time.Second)
	cancel()
	waitForEvent(t, rec, EventItemCanceled, "drop", 2*time.Second)

	// Remaining two items must still flush on the wait timer, unaffected.
	vc.Advance(100 * time.Millisecond)
	waitForResults(t, rec, 2, time.Second)
	if sizes := exec.batchSizes(); len(sizes) != 1 || sizes[0] != 2 {
		t.Fatalf("batch after cancel: %v, want one batch of 2", sizes)
	}
	var ids []string
	for _, it := range exec.batches[0] {
		ids = append(ids, it.ID)
	}
	if len(ids) != 2 || ids[0] != "keep-1" || ids[1] != "keep-2" {
		t.Fatalf("executed ids: %v, want [keep-1 keep-2]", ids)
	}

	r1 := mustGet(t, f1)
	r3 := mustGet(t, f3)
	if r1.Err != nil || string(r1.Output) != "ok:one" {
		t.Fatalf("keep-1 result: %q %v", r1.Output, r1.Err)
	}
	if r3.Err != nil || string(r3.Output) != "ok:three" {
		t.Fatalf("keep-2 result: %q %v", r3.Output, r3.Err)
	}
	if r1.BatchID != r3.BatchID {
		t.Fatal("siblings should share a batch")
	}

	// The canceled future resolves with context.Canceled and no output.
	res, err := f2.Get(context.Background())
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled item Get: res=%v err=%v, want context.Canceled", res, err)
	}
	if res != nil {
		t.Fatalf("canceled item must not produce a result, got %+v", res)
	}
}

// TestCancellation_AfterDispatchDoesNotChangeResult proves cancellation is a
// no-op for an item already executing: its real result still comes back.
func TestCancellation_AfterDispatchDoesNotChangeResult(t *testing.T) {
	exec := &fakeExec{delay: 50 * time.Millisecond}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 10, MaxBytes: 10_000, MaxWait: 5 * time.Millisecond}, exec)

	ctx, cancel := context.WithCancel(context.Background())
	fut, err := s.Submit(ctx, Item{ID: "late", Key: "model-h", Payload: []byte("late-payload")})
	if err != nil {
		t.Fatal(err)
	}
	waitForEventCount(t, rec, EventSubmitted, 1, time.Second)
	vc.Advance(5 * time.Millisecond) // dispatches; executor sleeps in wall time
	cancel()                         // too late: item is in flight

	res := mustGet(t, fut) // must return the executed result, not cancellation
	if res.Err != nil || string(res.Output) != "ok:late-payload" {
		t.Fatalf("in-flight item: out=%q err=%v", res.Output, res.Err)
	}
}

// TestDifferentKeys_NeverMixed checks the compatibility-key boundary.
func TestDifferentKeys_NeverMixed(t *testing.T) {
	exec := &fakeExec{}
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 100, MaxBytes: 1_000_000, MaxWait: 10 * time.Millisecond}, exec)

	fa, err := s.Submit(context.Background(), Item{ID: "a", Key: "model-a", Payload: []byte("aa")})
	if err != nil {
		t.Fatal(err)
	}
	fb, err := s.Submit(context.Background(), Item{ID: "b", Key: "model-b", Payload: []byte("bb")})
	if err != nil {
		t.Fatal(err)
	}
	waitForEventCount(t, rec, EventSubmitted, 2, time.Second)
	vc.Advance(10 * time.Millisecond)
	waitForResults(t, rec, 2, time.Second)
	if got := exec.callCount(); got != 2 {
		t.Fatalf("calls=%d want 2 (one per key)", got)
	}
	for _, b := range exec.batches {
		if len(b) != 1 {
			t.Fatalf("keys were mixed: batch %v", b)
		}
	}
	if res := mustGet(t, fa); string(res.Output) != "ok:aa" {
		t.Fatalf("a: %q", res.Output)
	}
	if res := mustGet(t, fb); string(res.Output) != "ok:bb" {
		t.Fatalf("b: %q", res.Output)
	}
}

// TestClose_FlushesPending checks shutdown drains pending batches with reason
// "shutdown" and delivers results.
func TestClose_FlushesPending(t *testing.T) {
	exec := &fakeExec{}
	vc := NewVirtualClock()
	rec := NewEventRecorder()
	s := New(Config{MaxItems: 100, MaxBytes: 1_000_000, MaxWait: time.Hour, Clock: vc, Sink: rec}, exec)

	fut, err := s.Submit(context.Background(), Item{ID: "last", Key: "model-i", Payload: []byte("bye")})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	if got := exec.callCount(); got != 1 {
		t.Fatalf("calls=%d want 1", got)
	}
	res := mustGet(t, fut)
	if res.Err != nil || string(res.Output) != "ok:bye" {
		t.Fatalf("drained item: %q %v", res.Output, res.Err)
	}
	var sawShutdown bool
	for _, e := range rec.OfType(EventBatchFlush) {
		if e.Reason == "shutdown" {
			sawShutdown = true
		}
	}
	if !sawShutdown {
		t.Fatal("missing shutdown flush event")
	}

	if _, err := s.Submit(context.Background(), Item{ID: "x", Key: "model-i", Payload: []byte("y")}); !errors.Is(err, ErrClosed) {
		t.Fatalf("submit after close: got %v want ErrClosed", err)
	}
}

// TestExecutorResultCountMismatch fails items individually on protocol error.
func TestExecutorResultCountMismatch(t *testing.T) {
	exec := ExecutorFunc(func(ctx context.Context, batchID string, items []Item) ([]*ItemResult, error) {
		return []*ItemResult{{Index: 0, BatchID: batchID, Output: []byte("only-one")}}, nil
	})
	s, vc, rec := newTestScheduler(t, Config{MaxItems: 10, MaxBytes: 1000, MaxWait: 5 * time.Millisecond}, exec)
	futs := submitN(t, s, "model-j", 2, 4)
	waitForEventCount(t, rec, EventSubmitted, 2, time.Second)
	vc.Advance(5 * time.Millisecond)
	waitForResults(t, rec, 2, time.Second)
	for i, fut := range futs {
		res := mustGet(t, fut)
		if res.Err == nil || !strings.Contains(res.Err.Error(), "executor returned 1 results for 2 items") {
			t.Fatalf("item %d err=%v, want count-mismatch error", i, res.Err)
		}
	}
}

// ExecutorFunc adapts a function to Executor (test helper).
type ExecutorFunc func(ctx context.Context, batchID string, items []Item) ([]*ItemResult, error)

func (f ExecutorFunc) Execute(ctx context.Context, batchID string, items []Item) ([]*ItemResult, error) {
	return f(ctx, batchID, items)
}

// --- shared helpers --------------------------------------------------------

func submitN(t *testing.T, s *Scheduler, key string, n, payloadSize int) []*Future {
	t.Helper()
	futs := make([]*Future, n)
	for i := 0; i < n; i++ {
		payload := []byte(fmt.Sprintf("%s-%d-%s", key, i, strings.Repeat("p", payloadSize)))
		fut, err := s.Submit(context.Background(), Item{
			ID:      fmt.Sprintf("%s-item-%d", key, i),
			Key:     key,
			Payload: payload,
		})
		if err != nil {
			t.Fatalf("submit %d: %v", i, err)
		}
		futs[i] = fut
	}
	return futs
}

func waitForEvent(t *testing.T, rec *EventRecorder, typ EventType, itemID string, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		for _, e := range rec.OfType(typ) {
			if e.ItemID == itemID {
				return
			}
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for event %s for %s", typ, itemID)
}

// waitForFlushes blocks until n batch_flush events have been recorded.
func waitForFlushes(t *testing.T, rec *EventRecorder, n int, timeout time.Duration) []Event {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if got := rec.OfType(EventBatchFlush); len(got) >= n {
			return got
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d flush events, have %d", n, len(rec.OfType(EventBatchFlush)))
	return nil
}

// waitForResults blocks until n item_result events exist. Tests advance the
// virtual clock first; executeBatch is synchronous in the runner and emits
// these before it loops again, so observing n results means the batch ran.
func waitForResults(t *testing.T, rec *EventRecorder, n int, timeout time.Duration) []Event {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if got := rec.OfType(EventItemResult); len(got) >= n {
			return got
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d result events, have %d", n, len(rec.OfType(EventItemResult)))
	return nil
}

// waitForEventCount waits until at least n events of typ are recorded.
func waitForEventCount(t *testing.T, rec *EventRecorder, typ EventType, n int, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if len(rec.OfType(typ)) >= n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d %s events, have %d", n, typ, len(rec.OfType(typ)))
}
