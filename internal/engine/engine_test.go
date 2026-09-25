package engine

import (
	"strings"
	"testing"
	"time"

	"logcluster/internal/synth"
)

// fixedClock is a deterministic monotonic clock local to the tests.
type fixedClock struct{ t time.Time }

func newClock() *fixedClock {
	return &fixedClock{t: time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)}
}
func (c *fixedClock) Now() time.Time {
	c.t = c.t.Add(time.Millisecond)
	return c.t
}

func TestIngestGroupsNumbersAndUUIDs(t *testing.T) {
	eng := New(DefaultConfig(), newClock())
	a, err := eng.Ingest("GET /u/550e8400-e29b-41d4-a716-446655440000 200 128")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := eng.Ingest("GET /u/660e8400-e29b-41d4-a716-446655440000 200 9001")
	if a.ClusterID != b.ClusterID {
		t.Fatalf("uuid+number variants split: %d vs %d", a.ClusterID, b.ClusterID)
	}
	if len(eng.Templates()) != 1 {
		t.Fatalf("templates = %d", len(eng.Templates()))
	}
	tpl := eng.Templates()[0].Template
	if !strings.Contains(tpl, "<UUID>") || !strings.Contains(tpl, "<NUM>") {
		t.Fatalf("template markers wrong: %s", tpl)
	}
}

func TestDifferentErrorsNeverMerge(t *testing.T) {
	eng := New(DefaultConfig(), newClock())
	e1, _ := eng.Ingest("connection refused to db host")
	e2, _ := eng.Ingest("connection timeout to db host")
	if e1.ClusterID == e2.ClusterID {
		t.Fatalf("refused/timeout merged into %d", e1.ClusterID)
	}
}

func TestWordParamPromotesWithVersion(t *testing.T) {
	eng := New(DefaultConfig(), newClock())
	eng.Ingest("task task101 ok")
	eng.Ingest("task task102 ok") // near miss -> promote
	eng.Ingest("task task103 ok") // matches <*>
	tpls := eng.Templates()
	if len(tpls) != 1 {
		t.Fatalf("templates = %d, want 1", len(tpls))
	}
	c := tpls[0]
	if c.Version != 2 {
		t.Fatalf("version = %d, want 2", c.Version)
	}
	if !strings.Contains(c.Template, "<*>") {
		t.Fatalf("promotion marker missing: %s", c.Template)
	}
	if len(c.Versions) != 2 {
		t.Fatalf("version history len = %d", len(c.Versions))
	}
	if c.Count != 3 {
		t.Fatalf("count = %d", c.Count)
	}
}

func TestCapacityEvictsLRU(t *testing.T) {
	cfg := Config{MaxClusters: 3, RingCapacity: 50, MaxLineBytes: 4096, MaxEvictedLog: 10}
	clk := newClock()
	eng := New(cfg, clk)

	// Three distinct templates.
	ids := make([]int, 3)
	for i := range ids {
		ev, _ := eng.Ingest("pattern alpha " + letter(i))
		ids[i] = ev.ClusterID
	}
	// Touch clusters 1 and 2 (and not 0) to make cluster 0 the LRU.
	clk.Now()
	eng.Ingest("pattern alpha " + letter(1))
	eng.Ingest("pattern alpha " + letter(2))

	// A fourth distinct template forces eviction.
	ev, _ := eng.Ingest("totally different shape here now")
	if ev.ClusterID == ids[0] {
		t.Fatal("new line assigned to evicted cluster")
	}
	stats := eng.Stats()
	if stats.ActiveClusters != 3 {
		t.Fatalf("active = %d, want 3", stats.ActiveClusters)
	}
	if stats.EvictedClusters != 1 {
		t.Fatalf("evicted = %d, want 1", stats.EvictedClusters)
	}
	evicted := eng.Evicted()
	if len(evicted) != 1 || evicted[0].ClusterID != ids[0] {
		t.Fatalf("wrong eviction record: %+v (want id %d)", evicted, ids[0])
	}
	// The evicted template no longer answers queries.
	if _, ok := eng.Cluster(ids[0]); ok {
		t.Fatal("evicted cluster still present")
	}
}

func TestEvictedLogBounded(t *testing.T) {
	cfg := Config{MaxClusters: 1, RingCapacity: 10, MaxLineBytes: 4096, MaxEvictedLog: 2}
	eng := New(cfg, newClock())
	for i := 0; i < 5; i++ {
		eng.Ingest("distinct shape number " + letter(i) + " here")
	}
	if got := len(eng.Evicted()); got > 2 {
		t.Fatalf("evicted log len = %d, cap 2", got)
	}
	if eng.Stats().EvictedClusters < 4 {
		t.Fatalf("total eviction counter wrong: %d", eng.Stats().EvictedClusters)
	}
}

func TestDeterminism(t *testing.T) {
	ds := synth.Build()
	run := func() []int {
		eng := New(DefaultConfig(), newClock())
		out := make([]int, 0, len(ds.Samples))
		for _, s := range ds.Samples {
			ev, err := eng.Ingest(s.Line)
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, ev.ClusterID)
		}
		return out
	}
	a := run()
	b := run()
	if len(a) != len(b) {
		t.Fatal("lengths differ")
	}
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("nondeterministic at %d: %d vs %d", i, a[i], b[i])
		}
	}
}

func TestEmptyAndLongLine(t *testing.T) {
	cfg := Config{MaxClusters: 10, RingCapacity: 10, MaxLineBytes: 64, MaxEvictedLog: 10}
	eng := New(cfg, newClock())
	if _, err := eng.Ingest("   "); err == nil {
		t.Fatal("empty line should error")
	}
	long := strings.Repeat("x", 10_000)
	ev, err := eng.Ingest(long)
	if err != nil {
		t.Fatal(err)
	}
	if len(ev.Line) != 64 {
		t.Fatalf("line clipped to %d bytes, want 64", len(ev.Line))
	}
}

func TestSnapshotRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/snap.json"
	eng := New(DefaultConfig(), newClock())
	for i := 0; i < 20; i++ {
		if _, err := eng.Ingest("ping host" + letter(i%5) + " count " + itoa(i)); err != nil {
			t.Fatal(err)
		}
	}
	if err := eng.Save(path); err != nil {
		t.Fatal(err)
	}

	eng2 := New(DefaultConfig(), newClock())
	loaded, err := eng2.LoadIfExists(path)
	if err != nil || !loaded {
		t.Fatalf("load: loaded=%v err=%v", loaded, err)
	}
	if len(eng2.Templates()) != len(eng.Templates()) {
		t.Fatalf("templates after load: %d vs %d", len(eng2.Templates()), len(eng.Templates()))
	}
	// Continuing ingestion assigns to restored clusters, not new ones.
	before := len(eng2.Templates())
	ev, _ := eng2.Ingest("ping host" + letter(0) + " count 99999")
	_ = ev
	if len(eng2.Templates()) != before {
		t.Fatalf("template count changed after matching restored template: %d -> %d", before, len(eng2.Templates()))
	}
	if eng2.Stats().TotalLines != eng.Stats().TotalLines+1 {
		t.Fatal("total lines counter not restored")
	}
}

func letter(i int) string {
	return string(rune('a' + i))
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}
