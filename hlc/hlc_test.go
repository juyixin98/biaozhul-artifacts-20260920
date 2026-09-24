package hlc

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

// fakeClock is a fully controllable physical time source. Tests never read
// the real wall clock, so assertions are deterministic.
type fakeClock struct {
	mu  sync.Mutex
	now int64
}

func newFakeClock(start int64) *fakeClock { return &fakeClock{now: start} }

func (f *fakeClock) get() int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *fakeClock) set(v int64) {
	f.mu.Lock()
	f.now = v
	f.mu.Unlock()
}

// advance waits: sleeping until the fake clock reaches target advances it by
// one millisecond (models a physical clock that catches up promptly).
func (f *fakeClock) advance(target int64, deadline time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.now < target {
		f.now = target
	}
	return nil
}

// failWait simulates a physical clock that cannot be advanced.
func failWait(target int64, deadline time.Time) error { return ErrOverflow }

func newTestClock(t *testing.T, node string, f *fakeClock, opts ...Option) *Clock {
	t.Helper()
	all := []Option{WithPhysicalFunc(f.get), WithWaitFunc(f.advance)}
	all = append(all, opts...)
	c, err := New(node, all...)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func TestTickAdvancesWithinSameMillisecond(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "a", f)

	var prev Timestamp
	for i := 1; i <= 5; i++ {
		ts, err := c.Tick()
		if err != nil {
			t.Fatalf("Tick %d: %v", i, err)
		}
		if ts.Physical != 1000 || ts.Logical != uint64(i) {
			t.Fatalf("Tick %d = %s, want (1000,%d)", i, ts.Wire(), i)
		}
		if i > 1 && !prev.Less(ts) {
			t.Fatalf("ordering broken: %s !< %s", prev.Wire(), ts.Wire())
		}
		prev = ts
	}
}

func TestTickResetsLogicalWhenPhysicalAdvances(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "a", f)

	_, _ = c.Tick()
	ts2, _ := c.Tick()
	if ts2.Logical != 2 {
		t.Fatalf("logical = %d, want 2", ts2.Logical)
	}
	f.set(1001)
	ts3, _ := c.Tick()
	if ts3.Physical != 1001 || ts3.Logical != 0 {
		t.Fatalf("after physical advance = %s, want (1001,0)", ts3.Wire())
	}
	if !ts2.Less(ts3) {
		t.Fatalf("%s !< %s after physical advance", ts2.Wire(), ts3.Wire())
	}
}

func TestBurstSameMillisecondStrictlyIncreasing(t *testing.T) {
	// Acceptance: a burst of events in one millisecond yields strictly
	// increasing timestamps; physical never moves.
	f := newFakeClock(5000)
	c := newTestClock(t, "burst", f)
	const n = 10_000
	out := make([]Timestamp, n)
	for i := range out {
		ts, err := c.Tick()
		if err != nil {
			t.Fatalf("Tick %d: %v", i, err)
		}
		out[i] = ts
	}
	for i := 1; i < n; i++ {
		if !out[i-1].Less(out[i]) {
			t.Fatalf("not strictly increasing at %d: %s !< %s",
				i, out[i-1].Wire(), out[i].Wire())
		}
	}
	last := out[n-1]
	if last.Physical != 5000 || last.Logical != n {
		t.Fatalf("last = %s, want physical 5000 logical %d", last.Wire(), n)
	}
}

func TestPhysicalClockRollbackKeepsMonotonicity(t *testing.T) {
	// Acceptance: wall clock jumps backwards; timestamps must keep increasing.
	f := newFakeClock(2000)
	c := newTestClock(t, "rb", f)

	before := make([]Timestamp, 0, 8)
	for i := 0; i < 3; i++ {
		ts, _ := c.Tick()
		before = append(before, ts)
	}
	f.set(1500) // backwards by 500ms
	for i := 0; i < 5; i++ {
		ts, err := c.Tick()
		if err != nil {
			t.Fatalf("Tick after rollback %d: %v", i, err)
		}
		before = append(before, ts)
	}
	for i := 1; i < len(before); i++ {
		if !before[i-1].Less(before[i]) {
			t.Fatalf("rollback broke ordering at %d: %s !< %s",
				i, before[i-1].Wire(), before[i].Wire())
		}
	}
	// While the wall clock is behind, the physical part stays pinned and
	// logical keeps climbing.
	if last := before[len(before)-1]; last.Physical != 2000 || last.Logical != 8 {
		t.Fatalf("last under rollback = %s, want (2000,8)", last.Wire())
	}
	// Recovery once the real clock catches up.
	f.set(2000)
	ts, _ := c.Tick()
	if ts.Physical != 2000 || ts.Logical != 9 {
		t.Fatalf("same ms tick = %s, want (2000,9)", ts.Wire())
	}
	f.set(2001)
	ts, _ = c.Tick()
	if ts.Physical != 2001 || ts.Logical != 0 {
		t.Fatalf("recovery tick = %s, want (2001,0)", ts.Wire())
	}
}

func TestReceiveMergesRemote(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "local", f)

	// Remote is ahead.
	got, err := c.Receive(Timestamp{Physical: 1500, Logical: 4, NodeID: "remote"})
	if err != nil {
		t.Fatal(err)
	}
	if got.Physical != 1500 || got.Logical != 5 {
		t.Fatalf("receive ahead = %s, want (1500,5)", got.Wire())
	}
	// Equal physical part, lower remote logical: local counter wins +1.
	got, _ = c.Receive(Timestamp{Physical: 1500, Logical: 0, NodeID: "remote2"})
	if got.Physical != 1500 || got.Logical != 6 {
		t.Fatalf("receive stale logical = %s, want (1500,6)", got.Wire())
	}
	// Equal physical part, higher remote logical: max then +1.
	got, _ = c.Receive(Timestamp{Physical: 1500, Logical: 99, NodeID: "remote3"})
	if got.Physical != 1500 || got.Logical != 100 {
		t.Fatalf("receive higher logical = %s, want (1500,100)", got.Wire())
	}
	// Remote behind on physical with a larger counter: local physical part
	// still dominates, so the remote counter is irrelevant.
	got, _ = c.Receive(Timestamp{Physical: 1499, Logical: 500, NodeID: "remote4"})
	if got.Physical != 1500 || got.Logical != 101 {
		t.Fatalf("receive behind = %s, want (1500,101)", got.Wire())
	}
}

func TestReceiveDuringRollback(t *testing.T) {
	f := newFakeClock(3000)
	c := newTestClock(t, "rb", f)

	first, _ := c.Receive(Timestamp{Physical: 4000, Logical: 10, NodeID: "r"})
	if first.Physical != 4000 || first.Logical != 11 {
		t.Fatalf("setup = %s want (4000,11)", first.Wire())
	}
	f.set(2500) // wall clock backwards
	ts, err := c.Receive(Timestamp{Physical: 3500, Logical: 0, NodeID: "r2"})
	if err != nil {
		t.Fatal(err)
	}
	// Local stored physical 4000 dominates; monotonicity preserved.
	if ts.Physical != 4000 || ts.Logical != 12 {
		t.Fatalf("receive during rollback = %s, want (4000,12)", ts.Wire())
	}
}

func TestCausalChainThreeNodes(t *testing.T) {
	// Acceptance: for a causal message chain a -> b -> c (with b's wall
	// clock behind a's), every delivered timestamp is strictly greater than
	// every prior one.
	fa, fb, fc := newFakeClock(10_000), newFakeClock(9_000), newFakeClock(9_500)
	a := newTestClock(t, "a", fa)
	b := newTestClock(t, "b", fb)
	cc := newTestClock(t, "c", fc)

	var chain []Timestamp
	// a sends m1
	m1, _ := a.Tick()
	chain = append(chain, m1)
	// b receives m1, replies m2
	r1, err := b.Receive(m1)
	if err != nil {
		t.Fatal(err)
	}
	chain = append(chain, r1)
	m2, _ := b.Tick()
	chain = append(chain, m2)
	// c receives m2 (c's wall clock behind), replies m3
	r2, err := cc.Receive(m2)
	if err != nil {
		t.Fatal(err)
	}
	chain = append(chain, r2)
	m3, _ := cc.Tick()
	chain = append(chain, m3)
	// a receives m3
	r3, err := a.Receive(m3)
	if err != nil {
		t.Fatal(err)
	}
	chain = append(chain, r3)

	for i := 1; i < len(chain); i++ {
		if !chain[i-1].Less(chain[i]) {
			t.Fatalf("causal chain not strictly increasing at %d:\n  %s !< %s",
				i, chain[i-1].Wire(), chain[i].Wire())
		}
	}
	// Expected shape:
	// m1 (10000,1) | b wall 9000 -> (10000,2) (10000,3) | c wall 9500
	// -> (10000,4) (10000,5) | a wall 10000 -> (10000,6)
	want := []struct {
		p int64
		l uint64
	}{
		{10000, 1}, {10000, 2}, {10000, 3},
		{10000, 4}, {10000, 5}, {10000, 6},
	}
	for i, w := range want {
		if chain[i].Physical != w.p || chain[i].Logical != w.l {
			t.Fatalf("chain[%d] = %s, want (%d,%d)", i, chain[i].Wire(), w.p, w.l)
		}
	}
}

func TestFutureDriftRejected(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "local", f, WithMaxDrift(100))

	snap := c.Peek()
	_, err := c.Receive(Timestamp{Physical: 1101, Logical: 0, NodeID: "far"})
	var drift *FutureDriftError
	if !errors.As(err, &drift) {
		t.Fatalf("want FutureDriftError, got %v", err)
	}
	if drift.RemotePhysical != 1101 || drift.LocalPhysical != 1000 || drift.MaxDrift != 100 {
		t.Fatalf("drift details wrong: %v", drift)
	}
	// Rejected message must not poison local state.
	if c.Peek() != snap {
		t.Fatalf("state changed after rejection: %s -> %s", snap.Wire(), c.Peek().Wire())
	}

	// Boundary: exactly MaxDrift ahead is accepted.
	ts, err := c.Receive(Timestamp{Physical: 1100, Logical: 0, NodeID: "edge"})
	if err != nil {
		t.Fatalf("boundary receive: %v", err)
	}
	if ts.Physical != 1100 || ts.Logical != 1 {
		t.Fatalf("boundary = %s, want (1100,1)", ts.Wire())
	}
}

func TestHugeFutureValueRejected(t *testing.T) {
	// Acceptance: a gigantic value (~year 292 million) is rejected, not merged.
	f := newFakeClock(1_700_000_000_000)
	c := newTestClock(t, "local", f, WithMaxDrift(1000))
	huge := Timestamp{Physical: 1<<62 - 1, Logical: 42, NodeID: "tyrant"}
	_, err := c.Receive(huge)
	if !errors.Is(err, ErrFutureDrift) {
		t.Fatalf("want ErrFutureDrift, got %v", err)
	}
	st := c.Stats()
	if st.DriftRejects != 1 {
		t.Fatalf("drift rejects = %d, want 1", st.DriftRejects)
	}
	// Clock still usable and still pinned near real physical time.
	ts, err := c.Tick()
	if err != nil {
		t.Fatal(err)
	}
	if ts.Physical != 1_700_000_000_000 || ts.Logical != 1 {
		t.Fatalf("tick after reject = %s", ts.Wire())
	}
}

func TestTickOverflowWaitsForPhysicalAdvance(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "ov", f, WithMaxLogical(3))

	var last Timestamp
	for i := 0; i < 3; i++ {
		ts, err := c.Tick()
		if err != nil {
			t.Fatal(err)
		}
		last = ts
	}
	if last.Logical != 3 {
		t.Fatalf("setup = %s, want logical 3", last.Wire())
	}
	// 4th tick saturates the counter: the fake clock advances and the
	// timestamp jumps to the new physical part.
	ts, err := c.Tick()
	if err != nil {
		t.Fatalf("saturated tick: %v", err)
	}
	if ts.Physical != 1001 || ts.Logical != 0 {
		t.Fatalf("post-overflow tick = %s, want (1001,0)", ts.Wire())
	}
	if !last.Less(ts) {
		t.Fatalf("overflow tick not greater: %s !< %s", last.Wire(), ts.Wire())
	}
	if c.Stats().OverflowWaits < 1 {
		t.Fatal("overflow wait not recorded")
	}
}

func TestTickOverflowWhenClockWillNotAdvance(t *testing.T) {
	f := newFakeClock(1000)
	c, err := New("stuck",
		WithPhysicalFunc(f.get),
		WithWaitFunc(failWait),
		WithMaxLogical(2),
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Tick(); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Tick(); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Tick(); !errors.Is(err, ErrOverflow) {
		t.Fatalf("saturated tick on stuck clock: want ErrOverflow, got %v", err)
	}
}

func TestReceiveOverflowWaitsThenMerges(t *testing.T) {
	// Local logical is at the limit; a remote timestamp with a huge logical
	// counter at the same physical part forces overflow handling.
	f := newFakeClock(1000)
	c := newTestClock(t, "ov", f, WithMaxLogical(5))
	for i := 0; i < 5; i++ {
		if _, err := c.Tick(); err != nil {
			t.Fatal(err)
		}
	}
	ts, err := c.Receive(Timestamp{Physical: 1000, Logical: 5, NodeID: "r"})
	if err != nil {
		t.Fatalf("receive at limit: %v", err)
	}
	// merge would need logical 6 > 5; clock waits to physical 1001, re-merges
	// (fresh physical dominates) -> (1001,0).
	if ts.Physical != 1001 || ts.Logical != 0 {
		t.Fatalf("post-overflow receive = %s, want (1001,0)", ts.Wire())
	}
}

func TestReceiveOverflowAtUint64LimitNoWraparound(t *testing.T) {
	// Regression: remote logical == uint64 max must NOT wrap "+1" to 0;
	// the merge must report overflow and advance the physical part.
	f := newFakeClock(1000)
	c := newTestClock(t, "wrap", f, WithMaxLogical(1<<64-1))
	ts, err := c.Receive(Timestamp{Physical: 2000, Logical: 1<<64 - 1, NodeID: "r"})
	if err != nil {
		t.Fatalf("receive at uint64 max: %v", err)
	}
	if ts.Physical != 2001 || ts.Logical != 0 {
		t.Fatalf("post-overflow = %s, want (2001,0) (no uint64 wraparound)", ts.Wire())
	}
}

func TestReceiveOverflowBoundedWaitDoesNotHang(t *testing.T) {
	// Regression: a remote timestamp pinned at the logical limit once caused
	// Receive to block until an arbitrarily distant physical time. With a
	// small wait budget it must fail fast with ErrOverflow.
	f := newFakeClock(1000)
	c, err := New("bounded",
		WithPhysicalFunc(f.get),
		WithWaitFunc(func(target int64, deadline time.Time) error {
			// Mimic defaultWait: honor the deadline instead of chasing target.
			if d := time.Until(deadline); d > 0 {
				time.Sleep(d)
			}
			return ErrOverflow
		}),
		WithMaxLogical(10),
		WithMaxOverflowWait(10*time.Millisecond),
	)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	_, err = c.Receive(Timestamp{Physical: 2000, Logical: 10, NodeID: "r"})
	if !errors.Is(err, ErrOverflow) {
		t.Fatalf("want ErrOverflow, got %v", err)
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("overflow wait took %s, expected fast failure", elapsed)
	}
}

func TestReceiveRejectsLogicalAboveLimit(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "l", f, WithMaxLogical(10))
	_, err := c.Receive(Timestamp{Physical: 1000, Logical: 11, NodeID: "r"})
	if !errors.Is(err, ErrInvalidTimestamp) {
		t.Fatalf("want ErrInvalidTimestamp, got %v", err)
	}
}

func TestInvalidTimestamps(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "l", f)
	bad := []Timestamp{
		{Physical: 1000, Logical: 0, NodeID: ""},
		{Physical: 1000, Logical: 0, NodeID: "a:b"},
		{Physical: 1000, Logical: 0, NodeID: "a b"},
		{Physical: -1, Logical: 0, NodeID: "r"},
	}
	for i, ts := range bad {
		if _, err := c.Receive(ts); !errors.Is(err, ErrInvalidTimestamp) {
			t.Fatalf("bad[%d] %+v: want ErrInvalidTimestamp, got %v", i, ts, err)
		}
	}
}

func TestConcurrentTicksAreUniqueAndOrdered(t *testing.T) {
	f := newFakeClock(7000)
	c := newTestClock(t, "par", f)

	const goroutines, per = 64, 200
	var wg sync.WaitGroup
	out := make(chan Timestamp, goroutines*per)
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < per; i++ {
				ts, err := c.Tick()
				if err != nil {
					t.Errorf("Tick: %v", err)
					return
				}
				out <- ts
			}
		}()
	}
	wg.Wait()
	close(out)

	seen := make(map[Timestamp]bool, goroutines*per)
	for ts := range out {
		if seen[ts] {
			t.Fatalf("duplicate timestamp issued: %s", ts.Wire())
		}
		seen[ts] = true
		if ts.Physical != 7000 || ts.Logical < 1 || ts.Logical > goroutines*per {
			t.Fatalf("unexpected %s", ts.Wire())
		}
	}
	if len(seen) != goroutines*per {
		t.Fatalf("got %d unique, want %d", len(seen), goroutines*per)
	}
}

func TestSerializationRoundTripNoPrecisionLoss(t *testing.T) {
	// Acceptance: full int64 / uint64 range values survive JSON and text
	// round-trips exactly. Physical uses a realistic far-future epoch; the
	// logical counter hits uint64 max.
	cases := []Timestamp{
		{Physical: 1_700_000_000_123, Logical: 1, NodeID: "a"},
		{Physical: 1<<62 - 1, Logical: 1<<64 - 1, NodeID: "node-42"},
		{Physical: 0, Logical: 0, NodeID: "z"},
		{Physical: 9_223_372_036_854_775_807, Logical: 18_446_744_073_709_551_615, NodeID: "max"},
	}
	for _, ts := range cases {
		s := ts.Wire()
		back, err := ParseWire(s)
		if err != nil {
			t.Fatalf("ParseWire(%q): %v", s, err)
		}
		if back != ts {
			t.Fatalf("wire round-trip mismatch: %+v != %+v", back, ts)
		}
	}

	// JSON object round-trip.
	for _, ts := range cases {
		data, err := ts.MarshalJSON()
		if err != nil {
			t.Fatal(err)
		}
		var back Timestamp
		if err := back.UnmarshalJSON(data); err != nil {
			t.Fatalf("unmarshal %s: %v", data, err)
		}
		if back != ts {
			t.Fatalf("json round-trip mismatch: %+v != %+v (%s)", back, ts, data)
		}
	}

	// Quoted-number form (IEEE-754-safe transport).
	quoted := []byte(fmt.Sprintf(
		`{"physical_ms":"%d","logical":"%d","node_id":"q"}`,
		int64(1<<62-1), uint64(1<<64-1)))
	var q Timestamp
	if err := q.UnmarshalJSON(quoted); err != nil {
		t.Fatal(err)
	}
	if q.Physical != 1<<62-1 || q.Logical != 1<<64-1 || q.NodeID != "q" {
		t.Fatalf("quoted parse lost precision: %s", q.Wire())
	}

	// JSON string carrying canonical text.
	var viaStr Timestamp
	if err := viaStr.UnmarshalJSON([]byte(`"hlc://str/12345:67890"`)); err != nil {
		t.Fatal(err)
	}
	if viaStr.Physical != 12345 || viaStr.Logical != 67890 || viaStr.NodeID != "str" {
		t.Fatalf("string wire parse: %+v", viaStr)
	}
}

func TestCompareOrdering(t *testing.T) {
	base := Timestamp{Physical: 100, Logical: 1, NodeID: "x"}
	if !base.Less(Timestamp{Physical: 100, Logical: 2, NodeID: "y"}) {
		t.Fatal("logical order")
	}
	if !base.Less(Timestamp{Physical: 101, Logical: 0}) {
		t.Fatal("physical order")
	}
	if base.Compare(Timestamp{Physical: 100, Logical: 1, NodeID: "z"}) != 0 {
		t.Fatal("node id must not affect ordering")
	}
}

func TestNowIsTickAndEqual(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "n", f)
	a, err := c.Now()
	if err != nil {
		t.Fatal(err)
	}
	b, err := c.Tick()
	if err != nil {
		t.Fatal(err)
	}
	if !a.Less(b) {
		t.Fatalf("Now then Tick not increasing: %s !< %s", a.Wire(), b.Wire())
	}
	if !a.Equal(Timestamp{Physical: a.Physical, Logical: a.Logical, NodeID: "other"}) {
		t.Fatal("Equal must ignore node id")
	}
	if a.Equal(b) {
		t.Fatal("distinct timestamps compared equal")
	}
}

func TestDefaultPhysicalIsUnixMillis(t *testing.T) {
	p := defaultPhysical()
	if p < 1_500_000_000_000 { // after year 2017
		t.Fatalf("defaultPhysical %d does not look like unix millis", p)
	}
}

func TestDefaultWaitReturnsWhenTargetReaches(t *testing.T) {
	// Target in the past: returns immediately, nil.
	if err := defaultWait(time.Now().Add(-time.Millisecond).UnixMilli(),
		time.Now().Add(time.Second)); err != nil {
		t.Fatalf("past target: %v", err)
	}
	// Target far future, tight deadline: ErrOverflow, fast.
	start := time.Now()
	err := defaultWait(time.Now().Add(time.Hour).UnixMilli(),
		time.Now().Add(20*time.Millisecond))
	if !errors.Is(err, ErrOverflow) {
		t.Fatalf("want ErrOverflow, got %v", err)
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("defaultWait took %s honoring deadline", d)
	}
}

func TestNewValidationAndDefaults(t *testing.T) {
	if _, err := New("", WithPhysicalFunc(func() int64 { return 1 })); err == nil {
		t.Fatal("empty node accepted")
	}
	f := newFakeClock(1)
	if _, err := New("x", WithPhysicalFunc(f.get), WithMaxDrift(-1)); err == nil {
		t.Fatal("negative drift accepted")
	}
	if _, err := New("x", WithPhysicalFunc(f.get), WithMaxLogical(0)); err == nil {
		t.Fatal("zero max logical accepted")
	}
	if _, err := New("x", WithPhysicalFunc(f.get),
		WithMaxOverflowWait(-1)); err == nil {
		t.Fatal("negative overflow wait accepted")
	}
	if _, err := New("x", WithPhysicalFunc(func() int64 { return -1 })); err == nil {
		t.Fatal("negative initial physical accepted")
	}
	c, err := New("x", WithPhysicalFunc(f.get))
	if err != nil {
		t.Fatal(err)
	}
	if c.MaxDrift() != DefaultMaxDrift || c.MaxLogical() != DefaultMaxLogical ||
		c.NodeID() != "x" {
		t.Fatal("defaults not applied")
	}
}
