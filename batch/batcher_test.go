package batch

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

// ---- 测试用 Request / Executor / 事件收集器 ----

type testItem struct {
	id   string
	key  string
	size int
	fail bool
}

func (i *testItem) Key() string         { return i.key }
func (i *testItem) Size() int           { return i.size }
func (i *testItem) SchedulerID() string { return i.id }

type batchRecord struct {
	ids   []string
	sizes []int
}

type recordingExec struct {
	mu      sync.Mutex
	batches []batchRecord
}

func (e *recordingExec) Execute(_ context.Context, items []*testItem) []ItemResult {
	rec := batchRecord{}
	res := make([]ItemResult, len(items))
	for i, it := range items {
		rec.ids = append(rec.ids, it.id)
		rec.sizes = append(rec.sizes, it.size)
		if it.fail {
			res[i] = ItemResult{Err: fmt.Errorf("simulated failure of %s", it.id)}
		} else {
			res[i] = ItemResult{Output: map[string]any{"id": it.id, "echo": it.size}}
		}
	}
	e.mu.Lock()
	e.batches = append(e.batches, rec)
	e.mu.Unlock()
	return res
}

func (e *recordingExec) snapshot() []batchRecord {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]batchRecord, len(e.batches))
	copy(out, e.batches)
	return out
}

type eventLog struct {
	mu     sync.Mutex
	events []Event
}

func (l *eventLog) onEvent(e Event) {
	l.mu.Lock()
	l.events = append(l.events, e)
	l.mu.Unlock()
}

func (l *eventLog) all() []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Event, len(l.events))
	copy(out, l.events)
	return out
}

func (l *eventLog) count(typ string) int {
	n := 0
	for _, e := range l.all() {
		if e.Type == typ {
			n++
		}
	}
	return n
}

func (l *eventLog) waitFor(t *testing.T, typ string, want int, timeout time.Duration) []Event {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		var got []Event
		for _, e := range l.all() {
			if e.Type == typ {
				got = append(got, e)
			}
		}
		if len(got) >= want {
			return got
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d %q events, got %d", want, typ, l.count(typ))
	return nil
}

// newHarness 组装虚拟时钟 + 事件收集器 + 录制执行器。
func newHarness(t *testing.T, cfg Config) (*Batcher[*testItem], *FakeClock, *recordingExec, *eventLog) {
	t.Helper()
	clk := NewFakeClock(time.Unix(1_700_000_000, 0))
	exec := &recordingExec{}
	log := &eventLog{}
	if cfg.Clock == nil {
		cfg.Clock = clk
	}
	cfg.Sink = EventSinkFunc(log.onEvent)
	b, err := New[*testItem](exec, cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = b.Shutdown(ctx)
	})
	return b, clk, exec, log
}

func baseCfg() Config {
	return Config{
		MaxCount:      4,
		MaxBatchBytes: 1000,
		MaxItemBytes:  500,
		MaxWait:       10 * time.Millisecond,
	}
}

type submitResult struct {
	id  string
	out map[string]any
	err error
}

func submitAsync(b *Batcher[*testItem], item *testItem) <-chan submitResult {
	ch := make(chan submitResult, 1)
	go func() {
		out, err := b.Submit(context.Background(), item)
		ch <- submitResult{id: item.id, out: out, err: err}
	}()
	return ch
}

// ---- 验收场景 1：低流量 -> 等待超时发批 ----

func TestAcceptance_LowTrafficTimeoutFlush(t *testing.T) {
	b, clk, exec, log := newHarness(t, baseCfg())

	r1 := submitAsync(b, &testItem{id: "r1", key: "model-a", size: 10})
	r2 := submitAsync(b, &testItem{id: "r2", key: "model-a", size: 20})

	// 两个请求都已入批，但远未达到满批条件。
	log.waitFor(t, EventItemAdmitted, 2, time.Second)
	if batches := exec.snapshot(); len(batches) != 0 {
		t.Fatalf("expected no batch before max_wait, got %d", len(batches))
	}

	// 时间推进到等待窗口的一半：仍不应发批。
	clk.Advance(5 * time.Millisecond)
	time.Sleep(10 * time.Millisecond)
	if batches := exec.snapshot(); len(batches) != 0 {
		t.Fatalf("expected no batch at half max_wait, got %d", len(batches))
	}

	// 越过 max_wait：按超时原因发出唯一一个批。
	clk.Advance(10 * time.Millisecond)
	flushed := log.waitFor(t, EventBatchFlushed, 1, time.Second)
	if flushed[0].Reason != ReasonTimeout {
		t.Fatalf("flush reason = %q, want %q", flushed[0].Reason, ReasonTimeout)
	}
	if flushed[0].Count != 2 {
		t.Fatalf("flush count = %d, want 2", flushed[0].Count)
	}

	// 每个请求独立核对各自结果。
	for _, ch := range []<-chan submitResult{r1, r2} {
		select {
		case res := <-ch:
			if res.err != nil {
				t.Fatalf("%s unexpected error: %v", res.id, res.err)
			}
			if res.out["id"] != res.id {
				t.Fatalf("%s got output id %v", res.id, res.out["id"])
			}
		case <-time.After(time.Second):
			t.Fatal("timeout waiting for submit result")
		}
	}

	batches := exec.snapshot()
	if len(batches) != 1 || len(batches[0].ids) != 2 {
		t.Fatalf("executor batches = %+v, want one batch of 2", batches)
	}
}

// ---- 验收场景 2：高流量 -> 满批即发，且不同模型互不混批 ----

func TestAcceptance_HighTrafficFullBatch(t *testing.T) {
	b, clk, exec, log := newHarness(t, baseCfg())

	// 模型 A 8 个请求 -> 2 个满批（4/批）；模型 B 5 个 -> 1 个满批 + 1 个超时批。
	var results []<-chan submitResult
	for i := 0; i < 8; i++ {
		results = append(results, submitAsync(b,
			&testItem{id: fmt.Sprintf("a%d", i), key: "model-a", size: 5}))
	}
	for i := 0; i < 5; i++ {
		results = append(results, submitAsync(b,
			&testItem{id: fmt.Sprintf("b%d", i), key: "model-b", size: 5}))
	}

	// A 的两个满批与 B 的一个满批（共 3 个，12 条），无需推进时钟。
	log.waitFor(t, EventBatchFlushed, 3, time.Second)

	clk.Advance(10 * time.Millisecond) // B 的剩余 1 条超时发批
	log.waitFor(t, EventBatchFlushed, 4, time.Second)

	// 收集并逐项核对。
	gotIDs := make(map[string]map[string]any)
	errIDs := make(map[string]error)
	for _, ch := range results {
		select {
		case res := <-ch:
			if res.err != nil {
				errIDs[res.id] = res.err
			} else {
				gotIDs[res.id] = res.out
			}
		case <-time.After(time.Second):
			t.Fatal("timeout waiting for submit result")
		}
	}
	if len(errIDs) != 0 {
		t.Fatalf("unexpected errors: %+v", errIDs)
	}
	if len(gotIDs) != 13 {
		t.Fatalf("got %d successful results, want 13", len(gotIDs))
	}
	for id, out := range gotIDs {
		if out["id"] != id {
			t.Fatalf("result for %s carries id %v", id, out["id"])
		}
	}

	batches := exec.snapshot()
	if len(batches) != 4 {
		t.Fatalf("got %d batches, want 4", len(batches))
	}
	var aSizes, bSizes []int
	for _, rec := range batches {
		switch rec.ids[0][:1] {
		case "a":
			aSizes = append(aSizes, len(rec.ids))
		case "b":
			bSizes = append(bSizes, len(rec.ids))
			if len(rec.ids) > 1 && rec.ids[0][:1] != rec.ids[1][:1] {
				t.Fatalf("batch mixed compatibility keys: %+v", rec.ids)
			}
		}
	}
	if len(aSizes) != 2 || aSizes[0] != 4 || aSizes[1] != 4 {
		t.Fatalf("model-a batch sizes = %v, want [4 4]", aSizes)
	}
	if sum(bSizes) != 5 {
		t.Fatalf("model-b total items = %v, want 5", bSizes)
	}

	reasons := flushReasons(log)
	if reasons[ReasonCount] != 3 || reasons[ReasonTimeout] != 1 {
		t.Fatalf("flush reasons = %+v, want max_count=3 max_wait=1", reasons)
	}
}

func sum(xs []int) int {
	n := 0
	for _, x := range xs {
		n += x
	}
	return n
}

func flushReasons(log *eventLog) map[string]int {
	out := map[string]int{}
	for _, e := range log.all() {
		if e.Type == EventBatchFlushed {
			out[e.Reason]++
		}
	}
	return out
}

// ---- 验收场景 3：批内部分失败，每个请求拿到各自独立结果 ----

func TestAcceptance_PartialFailureInBatch(t *testing.T) {
	b, clk, exec, log := newHarness(t, baseCfg())

	r1 := submitAsync(b, &testItem{id: "ok-1", key: "m", size: 1})
	r2 := submitAsync(b, &testItem{id: "bad-1", key: "m", size: 1, fail: true})
	r3 := submitAsync(b, &testItem{id: "ok-2", key: "m", size: 1})

	log.waitFor(t, EventItemAdmitted, 3, time.Second)
	// 不满足满批，直接推进到超时，使三者在同一批中一起执行。
	clk.Advance(10 * time.Millisecond)
	res := waitResult(t, r1, r2, r3)

	if len(exec.snapshot()) != 1 {
		t.Fatalf("want exactly one executed batch, got %+v", exec.snapshot())
	}
	if _, ok := res["ok-1"]; !ok {
		t.Fatalf("successful item ok-1 lost: %+v", res)
	}
	if _, ok := res["ok-2"]; !ok {
		t.Fatalf("successful item ok-2 lost: %+v", res)
	}
	if err := errOf(res, "bad-1"); err == nil {
		t.Fatal("failing item unexpectedly succeeded")
	}
	if err := errOf(res, "ok-1"); err != nil {
		t.Fatalf("ok-1 affected by neighbor failure: %v", err)
	}
	if err := errOf(res, "ok-2"); err != nil {
		t.Fatalf("ok-2 affected by neighbor failure: %v", err)
	}
	if log.count(EventItemSucceeded) != 2 || log.count(EventItemFailed) != 1 {
		t.Fatalf("item events = success %d fail %d, want 2/1",
			log.count(EventItemSucceeded), log.count(EventItemFailed))
	}
}

func waitResult(t *testing.T, chs ...<-chan submitResult) map[string]submitResult {
	t.Helper()
	// 时钟推进由调用方在调用前完成；这里只做接收。
	out := map[string]submitResult{}
	for _, ch := range chs {
		select {
		case r := <-ch:
			out[r.id] = r
		case <-time.After(time.Second):
			t.Fatal("timeout waiting for result")
		}
	}
	return out
}

func errOf(m map[string]submitResult, id string) error {
	r, ok := m[id]
	if !ok {
		return fmt.Errorf("missing result %s", id)
	}
	return r.err
}

// ---- 超大单项：明确拒绝，不影响同键其它请求 ----

func TestOversizedItemRejected(t *testing.T) {
	b, clk, exec, log := newHarness(t, baseCfg())

	big := &testItem{id: "big", key: "m", size: 9999}
	if _, err := b.Submit(context.Background(), big); !errors.Is(err, ErrItemTooLarge) {
		t.Fatalf("oversize err = %v, want ErrItemTooLarge", err)
	}
	tle, ok := AsItemTooLarge(func() error {
		_, err := b.Submit(context.Background(), big)
		return err
	}())
	if !ok || tle.Size != 9999 || tle.Max != 500 {
		t.Fatalf("ItemTooLargeError details = %+v ok=%v", tle, ok)
	}

	// 同键的正常请求照常合批、超时发批。
	r := submitAsync(b, &testItem{id: "small", key: "m", size: 10})
	log.waitFor(t, EventItemAdmitted, 1, time.Second)
	clk.Advance(10 * time.Millisecond)
	select {
	case got := <-r:
		if got.err != nil {
			t.Fatalf("normal item failed: %v", got.err)
		}
	case <-time.After(time.Second):
		t.Fatal("normal item did not complete")
	}

	st := b.Stats()
	if st.Rejected != 2 {
		t.Fatalf("rejected = %d, want 2", st.Rejected)
	}
	if len(exec.snapshot()) != 1 {
		t.Fatalf("oversized item must not enter a batch, got %+v", exec.snapshot())
	}
}

// ---- 取消只影响对应项 ----

func TestCancelOnlyAffectsOwnItem(t *testing.T) {
	b, clk, _, log := newHarness(t, baseCfg())

	ctx1, cancel1 := context.WithCancel(context.Background())
	ctx2 := context.Background()

	type res struct {
		out map[string]any
		err error
	}
	ch1 := make(chan res, 1)
	ch2 := make(chan res, 1)
	go func() {
		out, err := b.Submit(ctx1, &testItem{id: "canceled", key: "m", size: 10})
		ch1 <- res{out, err}
	}()
	go func() {
		out, err := b.Submit(ctx2, &testItem{id: "survivor", key: "m", size: 10})
		ch2 <- res{out, err}
	}()

	log.waitFor(t, EventItemAdmitted, 2, time.Second)
	cancel1()
	// 等待摘条事件被处理后再推进时钟，保证确定性。
	log.waitFor(t, EventItemCanceled, 1, time.Second)

	clk.Advance(10 * time.Millisecond)

	select {
	case got := <-ch1:
		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("canceled item err = %v, want context.Canceled", got.err)
		}
	case <-time.After(time.Second):
		t.Fatal("canceled item did not return")
	}
	select {
	case got := <-ch2:
		if got.err != nil || got.out["id"] != "survivor" {
			t.Fatalf("survivor result = %+v err=%v", got.out, got.err)
		}
	case <-time.After(time.Second):
		t.Fatal("survivor did not complete after neighbor cancel")
	}

	// 存活者必须独自成批发出（批大小为 1）。
	flushes := log.waitFor(t, EventBatchFlushed, 1, time.Second)
	if flushes[0].Count != 1 || flushes[0].Reason != ReasonTimeout {
		t.Fatalf("flush = %+v, want single-item timeout flush", flushes[0])
	}
}

// ---- 字节数约束触发发批 ----

func TestByteLimitFlushes(t *testing.T) {
	cfg := baseCfg()
	cfg.MaxBatchBytes = 100
	cfg.MaxItemBytes = 100
	b, clk, exec, log := newHarness(t, cfg)

	r1 := submitAsync(b, &testItem{id: "x1", key: "m", size: 60})
	r2 := submitAsync(b, &testItem{id: "x2", key: "m", size: 60})

	first := log.waitFor(t, EventBatchFlushed, 1, time.Second)
	if first[0].Reason != ReasonBytes || first[0].Bytes != 60 {
		t.Fatalf("first flush = %+v, want max_bytes/60", first[0])
	}
	log.waitFor(t, EventItemAdmitted, 2, time.Second)
	clk.Advance(10 * time.Millisecond)
	log.waitFor(t, EventBatchFlushed, 2, time.Second)

	for _, ch := range []<-chan submitResult{r1, r2} {
		select {
		case got := <-ch:
			if got.err != nil {
				t.Fatalf("%s err = %v", got.id, got.err)
			}
		case <-time.After(time.Second):
			t.Fatal("result timeout")
		}
	}
	if sizes := exec.snapshot(); len(sizes) != 2 || sizes[0].sizes[0] != 60 {
		t.Fatalf("batches = %+v, want two 60-byte batches", sizes)
	}
}

// ---- MaxCount=1：单项即满，无需等待时钟 ----

func TestMaxCountOneFlushesImmediately(t *testing.T) {
	cfg := baseCfg()
	cfg.MaxCount = 1
	b, clk, exec, log := newHarness(t, cfg)

	r := submitAsync(b, &testItem{id: "solo", key: "m", size: 1})
	flushed := log.waitFor(t, EventBatchFlushed, 1, time.Second)
	if flushed[0].Reason != ReasonCount || flushed[0].Count != 1 {
		t.Fatalf("flush = %+v, want max_count with count=1", flushed[0])
	}
	// 不推进时钟也必须拿到结果。
	select {
	case got := <-r:
		if got.err != nil || got.out["id"] != "solo" {
			t.Fatalf("result = %+v err=%v", got.out, got.err)
		}
	case <-time.After(time.Second):
		t.Fatal("single-item batch did not complete without clock advance")
	}
	_ = clk
	if sizes := exec.snapshot(); len(sizes) != 1 || sizes[0].sizes[0] != 1 {
		t.Fatalf("batches = %+v, want one size-1 batch", sizes)
	}
}

// ---- 关闭：在途请求排空，关闭后拒绝新请求 ----

func TestShutdownDrains(t *testing.T) {
	cfg := baseCfg()
	clk := NewFakeClock(time.Unix(1_700_000_000, 0))
	exec := &recordingExec{}
	log := &eventLog{}
	cfg.Clock = clk
	cfg.Sink = EventSinkFunc(log.onEvent)
	b, err := New[*testItem](exec, cfg)
	if err != nil {
		t.Fatal(err)
	}

	ch := submitAsync(b, &testItem{id: "draining", key: "m", size: 10})
	log.waitFor(t, EventItemAdmitted, 1, time.Second)

	shutdownErr := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		shutdownErr <- b.Shutdown(ctx)
	}()

	select {
	case got := <-ch:
		if got.err != nil {
			t.Fatalf("drained item err = %v", got.err)
		}
	case <-time.After(time.Second):
		t.Fatal("drained item did not complete")
	}
	select {
	case err := <-shutdownErr:
		if err != nil {
			t.Fatalf("shutdown err = %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("shutdown did not return")
	}

	if _, err := b.Submit(context.Background(), &testItem{id: "late", key: "m", size: 1}); !errors.Is(err, ErrShuttingDown) {
		t.Fatalf("post-shutdown submit err = %v, want ErrShuttingDown", err)
	}
	if got := flushReasons(log); got[ReasonDrain] != 1 {
		t.Fatalf("flush reasons = %+v, want one drain flush", got)
	}
}

// ---- 配置校验 ----
func TestConfigValidation(t *testing.T) {
	bad := []Config{
		baseCfg(), baseCfg(), baseCfg(), baseCfg(),
	}
	bad[0].MaxCount = 0
	bad[1].MaxBatchBytes = 0
	bad[2].MaxItemBytes = 0
	bad[3].MaxItemBytes = 100000
	for i, cfg := range bad {
		if _, err := New[*testItem](&recordingExec{}, cfg); err == nil {
			t.Fatalf("case %d: expected validation error", i)
		}
	}
}
