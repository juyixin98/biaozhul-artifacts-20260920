package ring

import "testing"

func TestEvictionOldestFirst(t *testing.T) {
	r := New(3)
	for i := int64(1); i <= 5; i++ {
		r.Add(Event{Seq: 0, Line: string(rune('a' - 1 + i))})
	}
	got := r.Snapshot()
	if len(got) != 3 {
		t.Fatalf("len = %d", len(got))
	}
	if got[0].Line != "c" || got[1].Line != "d" || got[2].Line != "e" {
		t.Fatalf("eviction order wrong: %v", got)
	}
	// Seq assigned monotonically even after wrap.
	if got[0].Seq != 3 || got[2].Seq != 5 {
		t.Fatalf("seq = %d,%d", got[0].Seq, got[2].Seq)
	}
}

func TestQueryNewestFirstAndFilter(t *testing.T) {
	r := New(10)
	for i := 0; i < 5; i++ {
		r.Add(Event{Line: "err boom", ClusterID: 1})
		r.Add(Event{Line: "ok fine", ClusterID: 2})
	}
	got := r.Query(1, true, "", 100)
	if len(got) != 5 {
		t.Fatalf("cluster filter: %d", len(got))
	}
	if got[0].Seq < got[len(got)-1].Seq {
		t.Fatal("query should be newest-first")
	}
	sub := r.Query(0, false, "boom", 100)
	if len(sub) != 5 {
		t.Fatalf("substring filter: %d", len(sub))
	}
}
