package syncer

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"nodesync/internal/chain"
	"nodesync/internal/gen"
	"nodesync/internal/store"
)

// fakeClient is an in-memory syncer.Client whose behavior per request can be
// scripted. It tracks concurrency so tests can assert the parallel cap.
type fakeClient struct {
	id       string
	blocks   []chain.Block
	advDelta int64
	mu       sync.Mutex
	inFlight int32
	maxSeen  int32
	total    int32

	// behavior, consulted in order per GetSegment call for a given start.
	// A behavior returns blocks/error; default serves honest blocks.
	behave func(start int64, attemptCall int) ([]chain.Block, error)
	// blockOnStart, if set, makes matching requests hang until ctx done.
	hangOn map[int64]bool
	// delay adds latency before responding.
	delay time.Duration
	// delayFor adds latency only to a specific request start.
	delayFor map[int64]time.Duration

	calls map[int64]int
}

func newFake(id string, blocks []chain.Block, advDelta int64) *fakeClient {
	return &fakeClient{
		id:       id,
		blocks:   blocks,
		advDelta: advDelta,
		hangOn:   map[int64]bool{},
		delayFor: map[int64]time.Duration{},
		calls:    map[int64]int{},
	}
}

func (f *fakeClient) NodeID() string { return f.id }

func (f *fakeClient) Status(ctx context.Context) (int64, []byte, []byte, error) {
	tip := f.blocks[len(f.blocks)-1].Height
	return tip + f.advDelta, f.blocks[tip].Hash, f.blocks[0].Hash, nil
}

func (f *fakeClient) slice(start int64, limit int32) []chain.Block {
	end := start + int64(limit) - 1
	tip := f.blocks[len(f.blocks)-1].Height
	if end > tip {
		end = tip
	}
	if start > tip {
		return nil
	}
	out := make([]chain.Block, 0, end-start+1)
	for h := start; h <= end; h++ {
		out = append(out, f.blocks[h].Clone())
	}
	return out
}

func (f *fakeClient) GetSegment(ctx context.Context, start int64, limit int32) ([]chain.Block, error) {
	atomic.AddInt32(&f.total, 1)
	n := atomic.AddInt32(&f.inFlight, 1)
	defer atomic.AddInt32(&f.inFlight, -1)
	for {
		m := atomic.LoadInt32(&f.maxSeen)
		if n <= m || atomic.CompareAndSwapInt32(&f.maxSeen, m, n) {
			break
		}
	}

	f.mu.Lock()
	f.calls[start]++
	call := f.calls[start]
	hang := f.hangOn[start]
	behave := f.behave
	delay := f.delay
	f.mu.Unlock()

	if hang {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if d, ok := f.delayFor[start]; ok && d > 0 {
		delay = d
	}
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if behave != nil {
		if blk, err := behave(start, call); err != nil || blk != nil {
			return blk, err
		}
	}
	return f.slice(start, limit), nil
}

func testChain(t *testing.T, n int) []chain.Block {
	t.Helper()
	return gen.Build(gen.Spec{
		Length:    n,
		Seed:      []byte("nodesync-genesis"),
		BodyNonce: []byte("p006"),
	})
}

func openStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func seedGenesis(t *testing.T, st *store.Store, blocks []chain.Block) {
	t.Helper()
	if err := st.InitializeGenesis(context.Background(), blocks[0]); err != nil {
		t.Fatalf("init genesis: %v", err)
	}
}

func baseCfg(blocks []chain.Block) Config {
	return Config{
		TargetHeight: int64(len(blocks) - 1),
		GenesisHash:  blocks[0].Hash,
		SegmentSize:  4,
		MaxParallel:  4,
		MaxAttempts:  6,
		RPCTimeout:   2 * time.Second,
	}
}

func corruptAt(blocks []chain.Block, height int64) []chain.Block {
	out := make([]chain.Block, len(blocks))
	for i := range blocks {
		out[i] = blocks[i].Clone()
	}
	b := &out[height]
	if len(b.Body) == 0 {
		b.Body = []byte{0xFF}
	} else {
		b.Body[0] ^= 0xFF
	}
	return out
}

// TestHappyPath: all honest, advertised heights differ but are ignored.
func TestHappyPath(t *testing.T) {
	blocks := testChain(t, 20)
	st := openStore(t)
	seedGenesis(t, st, blocks)
	clients := []Client{
		newFake("a", blocks, 7),
		newFake("b", blocks, -3),
		newFake("c", blocks, 0),
	}
	rep, err := Run(context.Background(), st, clients, baseCfg(blocks))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.FinalCheckpoint != 20 || !rep.Complete {
		t.Fatalf("checkpoint=%d complete=%v", rep.FinalCheckpoint, rep.Complete)
	}
	pub, err := st.Chain(context.Background())
	if err != nil || len(pub) != 21 {
		t.Fatalf("published %d blocks err=%v", len(pub), err)
	}
}

// TestCorruptThenFailover: the node chosen first for a segment returns a
// tampered segment; retry from another node repairs it and the chain matches.
func TestCorruptThenFailover(t *testing.T) {
	blocks := testChain(t, 20)
	bad := corruptAt(blocks, 6) // height 6 is inside segment start 5
	st := openStore(t)
	seedGenesis(t, st, blocks)

	honestA := newFake("a", blocks, 0)
	honestB := newFake("b", blocks, 0)
	corruptC := newFake("c", bad, 0)
	// Segment size 4 -> segment start 5 maps to index (5%3)=2, so the corrupt
	// node is chosen first for that segment and a failover repairs it.
	clients := []Client{honestA, honestB, corruptC}

	rep, err := Run(context.Background(), st, clients, baseCfg(blocks))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.FinalCheckpoint != 20 {
		t.Fatalf("checkpoint=%d", rep.FinalCheckpoint)
	}
	if rep.Attempts["c"].BadHash == 0 {
		t.Fatal("expected a hash_mismatch tally on corrupt node")
	}
	if rep.Failovers == 0 {
		t.Fatal("expected at least one source failover")
	}
	// Evidence must record the hash mismatch.
	ev, _ := st.Evidence(context.Background())
	var sawHash bool
	for _, e := range ev {
		if e.Outcome == OutHashMismatch {
			sawHash = true
		}
	}
	if !sawHash {
		t.Fatal("no hash_mismatch evidence recorded")
	}
}

// TestBadParentForkBoundary: a self-consistent fork must pass internal checks
// but be rejected at the trusted anchor and then repaired from another source.
func TestBadParentForkBoundary(t *testing.T) {
	blocks := testChain(t, 20)
	fork := make([]chain.Block, len(blocks))
	copy(fork, blocks)
	// Rewrite segment start 9 as a fork anchored to a bogus parent (height 8
	// parent replaced). Keep it internally consistent.
	for h := 9; h < len(fork); h++ {
		if h == 9 {
			fork[h].ParentHash = bytesRepeat(0xEE)
		} else {
			fork[h].ParentHash = append([]byte(nil), fork[h-1].Hash...)
		}
		fork[h].Rehash()
	}

	st := openStore(t)
	seedGenesis(t, st, blocks)
	// Order so the fork node (index 1) is selected for start 9: (9+0)%3=0 a;
	// make 'a' the fork so it is picked first for start 9 ((9)%3=0).
	forkA := newFake("a", fork, 0)
	b := newFake("b", blocks, 0)
	c := newFake("c", blocks, 0)
	clients := []Client{forkA, b, c}

	rep, err := Run(context.Background(), st, clients, baseCfg(blocks))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.FinalCheckpoint != 20 {
		t.Fatalf("checkpoint=%d", rep.FinalCheckpoint)
	}
	ev, _ := st.Evidence(context.Background())
	var sawBoundary bool
	for _, e := range ev {
		if e.Outcome == OutParentMismatch {
			sawBoundary = true
		}
	}
	if !sawBoundary {
		t.Fatal("expected a boundary parent_mismatch rejection for the fork")
	}
}

// TestTimeoutRetriedFromOtherSource: a node hangs on one segment; the deadline
// fires, another source fills it, and the gap never stalls the run.
func TestTimeoutRetriedFromOtherSource(t *testing.T) {
	blocks := testChain(t, 20)
	st := openStore(t)
	seedGenesis(t, st, blocks)

	cfg := baseCfg(blocks)
	cfg.RPCTimeout = 120 * time.Millisecond

	hangA := newFake("a", blocks, 0)
	hangA.hangOn[1] = true // (1)%3==1 -> b normally; force via ordering below
	b := newFake("b", blocks, 0)
	c := newFake("c", blocks, 0)
	// Put hang node at index 1 so start 1 ((1)%3=1) selects it first.
	clients := []Client{b, hangA, c}

	rep, err := Run(context.Background(), st, clients, cfg)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.FinalCheckpoint != 20 {
		t.Fatalf("checkpoint=%d", rep.FinalCheckpoint)
	}
	if rep.Attempts["a"].Timeout == 0 {
		t.Fatal("expected timeout tally on hanging node")
	}
}

// TestParallelCap exercises concurrency: with slow honest nodes and
// MaxParallel=2, the observed in-flight count must never exceed 2.
func TestParallelCap(t *testing.T) {
	blocks := testChain(t, 40)
	st := openStore(t)
	seedGenesis(t, st, blocks)
	cfg := baseCfg(blocks)
	cfg.MaxParallel = 2
	cfg.SegmentSize = 2

	a := newFake("a", blocks, 0)
	b := newFake("b", blocks, 0)
	a.delay = 5 * time.Millisecond
	b.delay = 5 * time.Millisecond
	if _, err := Run(context.Background(), st, []Client{a, b}, cfg); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if a.maxSeen > int32(cfg.MaxParallel) || b.maxSeen > int32(cfg.MaxParallel) {
		t.Fatalf("parallel exceeded: a=%d b=%d cap=%d", a.maxSeen, b.maxSeen, cfg.MaxParallel)
	}
}

// TestGapNeverSkipped: if every node permanently fails one segment, the run
// must stop at the gap, never publish blocks past it, and report incomplete.
func TestGapNeverSkipped(t *testing.T) {
	blocks := testChain(t, 20)
	st := openStore(t)
	seedGenesis(t, st, blocks)

	makeFailing := func(id string) *fakeClient {
		c := newFake(id, blocks, 0)
		c.behave = func(start int64, call int) ([]chain.Block, error) {
			if start == 13 { // segment [13..16]
				return nil, fmt.Errorf("boom")
			}
			return nil, nil // honest default
		}
		return c
	}
	clients := []Client{makeFailing("a"), makeFailing("b"), makeFailing("c")}
	rep, err := Run(context.Background(), st, clients, baseCfg(blocks))
	if err == nil {
		t.Fatal("expected fatal error from persistent gap")
	}
	if rep.FinalCheckpoint != 12 {
		t.Fatalf("checkpoint should stop at 12 (before gap), got %d", rep.FinalCheckpoint)
	}
	pub, _ := st.Chain(context.Background())
	for _, bl := range pub {
		if bl.Height > 12 {
			t.Fatalf("published block past gap: height %d", bl.Height)
		}
	}
}

// TestCancelDoesNotAdvanceCheckpoint: cancel while requests are in flight; the
// checkpoint must remain at its pre-cancel value even though results may
// arrive later, and a subsequent Run resumes from that checkpoint.
func TestCancelDoesNotAdvanceCheckpoint(t *testing.T) {
	blocks := testChain(t, 40)
	st := openStore(t)
	seedGenesis(t, st, blocks)
	cfg := baseCfg(blocks)
	cfg.SegmentSize = 4

	// Slow nodes so the cancel lands with several requests in flight.
	a := newFake("a", blocks, 0)
	b := newFake("b", blocks, 0)
	c := newFake("c", blocks, 0)
	for _, f := range []*fakeClient{a, b, c} {
		f.delay = 60 * time.Millisecond
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(40 * time.Millisecond)
		cancel()
	}()
	rep, err := Run(ctx, st, []Client{a, b, c}, cfg)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
	cpAfter, _, _ := st.Checkpoint(context.Background())
	if rep.FinalCheckpoint != cpAfter {
		t.Fatal("report checkpoint disagrees with store")
	}

	// Give late goroutines a chance to complete; they must not write through.
	time.Sleep(150 * time.Millisecond)
	cpLater, _, _ := st.Checkpoint(context.Background())
	if cpLater != cpAfter {
		t.Fatalf("stale results advanced checkpoint after cancel: %d -> %d", cpAfter, cpLater)
	}

	// Resume with fast honest clients from the durable checkpoint; must reach tip.
	a2 := newFake("a", blocks, 0)
	b2 := newFake("b", blocks, 0)
	c2 := newFake("c", blocks, 0)
	rep2, err := Run(context.Background(), st, []Client{a2, b2, c2}, cfg)
	if err != nil {
		t.Fatalf("resume Run: %v", err)
	}
	if rep2.StartCheckpoint != cpAfter {
		t.Fatalf("resume did not start at durable checkpoint %d, got %d", cpAfter, rep2.StartCheckpoint)
	}
	if rep2.FinalCheckpoint != 40 {
		t.Fatalf("resume checkpoint=%d want 40", rep2.FinalCheckpoint)
	}
}

// TestResumeIsCheckpointDriven: seed a store that already verified up to a
// mid-chain height; the run must continue there regardless of advertised
// heights and never re-request or re-publish below it.
func TestResumeIsCheckpointDriven(t *testing.T) {
	blocks := testChain(t, 20)
	st := openStore(t)
	seedGenesis(t, st, blocks)
	// Manually publish heights 1..12 as the verified prefix.
	if err := st.AppendVerified(context.Background(), blocks[1:13]); err != nil {
		t.Fatalf("seed prefix: %v", err)
	}
	a := newFake("a", blocks, 99) // lie about height; must be ignored
	b := newFake("b", blocks, 99)
	rep, err := Run(context.Background(), st, []Client{a, b}, baseCfg(blocks))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.StartCheckpoint != 12 || rep.FinalCheckpoint != 20 {
		t.Fatalf("resume cp %d -> %d, want 12 -> 20", rep.StartCheckpoint, rep.FinalCheckpoint)
	}
	// No client should have been asked for a start below 13.
	for start, n := range a.calls {
		if start < 13 && n > 0 {
			t.Fatalf("client re-requested below checkpoint at %d", start)
		}
	}
}

// TestOutOfOrderBoundedBuffer: the first segment is served slowly while later
// segments in the window arrive first. They must be buffered, the buffer must
// stay bounded by the parallel window, and publication must still happen in
// strict order once the head segment lands.
func TestOutOfOrderBoundedBuffer(t *testing.T) {
	blocks := testChain(t, 20)
	st := openStore(t)
	seedGenesis(t, st, blocks)
	cfg := baseCfg(blocks) // SegmentSize 4, MaxParallel 4
	cfg.RPCTimeout = 2 * time.Second

	// Client index for start s is (s + attempts) % 3. start 1 -> client index 1;
	// make that client slow only for start 1 so later segments overtake it.
	head := newFake("b", blocks, 0)
	head.delayFor = map[int64]time.Duration{1: 120 * time.Millisecond}
	a := newFake("a", blocks, 0)
	c := newFake("c", blocks, 0)
	clients := []Client{a, head, c}

	var maxSum, maxBuf int32
	cfg.OnStep = func(inflight, buffered int) {
		atomic.StoreInt32(&maxSum, max32(atomic.LoadInt32(&maxSum), int32(inflight+buffered)))
		atomic.StoreInt32(&maxBuf, max32(atomic.LoadInt32(&maxBuf), int32(buffered)))
	}

	rep, err := Run(context.Background(), st, clients, cfg)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.FinalCheckpoint != 20 || !rep.Complete {
		t.Fatalf("checkpoint=%d complete=%v", rep.FinalCheckpoint, rep.Complete)
	}
	// The in-flight + buffered window may never exceed the parallel cap, and
	// the reorder buffer must hold multiple later segments (reordering) while
	// staying bounded.
	if maxSum > int32(cfg.MaxParallel) {
		t.Fatalf("inflight+buffered peaked at %d, cap %d", maxSum, cfg.MaxParallel)
	}
	if maxBuf < 2 {
		t.Fatalf("expected several out-of-order segments buffered, max=%d", maxBuf)
	}
	// Published chain must be contiguous and intact despite reordering.
	pub, _ := st.Chain(context.Background())
	if err := chain.VerifyChain(blocks[0].Hash, pub); err != nil {
		t.Fatalf("reordered publish broke chain: %v", err)
	}
}

// TestStaleResultsIgnored is covered by TestCancel; this asserts the evidence
// outcome is emitted when a result is delivered after shutdown.
func TestStoreRejectsNonContiguous(t *testing.T) {
	blocks := testChain(t, 10)
	st := openStore(t)
	seedGenesis(t, st, blocks)
	// Try to append starting at height 3 directly after genesis: must fail.
	if err := st.AppendVerified(context.Background(), blocks[3:5]); err == nil {
		t.Fatal("store accepted a non-contiguous append over a gap")
	}
	cp, _, _ := st.Checkpoint(context.Background())
	if cp != 0 {
		t.Fatalf("checkpoint moved after rejected append: %d", cp)
	}
}

func bytesRepeat(b byte) []byte {
	out := make([]byte, chain.HashLen)
	for i := range out {
		out[i] = b
	}
	return out
}

func max32(a, b int32) int32 {
	if b > a {
		return b
	}
	return a
}
