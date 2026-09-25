package store_test

import (
	"errors"
	"path/filepath"
	"testing"
	"time"

	"histmerge/internal/histogram"
	"histmerge/internal/store"
)

func labels() []store.Label {
	return []store.Label{{Name: "service", Value: "api"}, {Name: "route", Value: "/pay"}}
}

func hAt(total uint64, sum float64, counts ...uint64) *histogram.Histogram {
	uppers := []float64{1, 2, 4}
	h := &histogram.Histogram{TotalCount: total, Sum: sum}
	var run uint64
	for i, c := range counts {
		run += c
		var ub histogram.Bound
		if i < len(uppers) {
			ub = histogram.FiniteBound(uppers[i])
		} else {
			ub = histogram.InfBound()
		}
		h.Buckets = append(h.Buckets, histogram.Bucket{Upper: ub, CumulativeCount: run})
	}
	return h
}

func TestIngest_MonotonicAccepted(t *testing.T) {
	st, err := store.New("")
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	s1 := store.Sample{Timestamp: t0, Labels: labels(), Histogram: hAt(2, 2, 1, 1, 0, 0)}
	s2 := store.Sample{Timestamp: t0.Add(time.Minute), Labels: labels(), Histogram: hAt(5, 8, 2, 1, 1, 1)}
	if err := st.Ingest(s1); err != nil {
		t.Fatal(err)
	}
	if err := st.Ingest(s2); err != nil {
		t.Fatalf("monotonic sample rejected: %v", err)
	}
}

func TestIngest_CounterResetTotalRejected(t *testing.T) {
	st, _ := store.New("")
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	_ = st.Ingest(store.Sample{Timestamp: t0, Labels: labels(), Histogram: hAt(5, 8, 2, 1, 1, 1)})
	err := st.Ingest(store.Sample{Timestamp: t0.Add(time.Minute), Labels: labels(), Histogram: hAt(3, 5, 1, 1, 1, 0)})
	if !errors.Is(err, store.ErrCounterReset) {
		t.Fatalf("want ErrCounterReset, got %v", err)
	}
}

func TestIngest_CounterResetBucketRejected(t *testing.T) {
	st, _ := store.New("")
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	_ = st.Ingest(store.Sample{Timestamp: t0, Labels: labels(), Histogram: hAt(5, 8, 2, 1, 1, 1)})
	// Total grows 5->6 but bucket <=1 shrinks 2->1.
	err := st.Ingest(store.Sample{Timestamp: t0.Add(time.Minute), Labels: labels(),
		Histogram: hAt(6, 10, 1, 2, 2, 1)})
	if !errors.Is(err, store.ErrCounterReset) {
		t.Fatalf("bucket-level reset must be rejected, got %v", err)
	}
}

func TestIngest_OutOfOrderRejected(t *testing.T) {
	st, _ := store.New("")
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	_ = st.Ingest(store.Sample{Timestamp: t0, Labels: labels(), Histogram: hAt(2, 2, 1, 1, 0, 0)})
	err := st.Ingest(store.Sample{Timestamp: t0.Add(-time.Minute), Labels: labels(), Histogram: hAt(3, 4, 1, 1, 1, 0)})
	if !errors.Is(err, store.ErrOutOfOrder) {
		t.Fatalf("want ErrOutOfOrder, got %v", err)
	}
}

func TestIngest_InvalidHistogramRejected(t *testing.T) {
	st, _ := store.New("")
	bad := &histogram.Histogram{TotalCount: 3, Buckets: []histogram.Bucket{
		{Upper: histogram.FiniteBound(1), CumulativeCount: 3},
		// no +Inf bucket
	}}
	err := st.Ingest(store.Sample{Timestamp: time.Now(), Labels: labels(), Histogram: bad})
	if !errors.Is(err, histogram.ErrInvalidHistogram) {
		t.Fatalf("want ErrInvalidHistogram, got %v", err)
	}
}

func TestLatest_AndSelector(t *testing.T) {
	st, _ := store.New("")
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	l1 := []store.Label{{Name: "service", Value: "a"}}
	l2 := []store.Label{{Name: "service", Value: "b"}}
	_ = st.Ingest(store.Sample{Timestamp: t0, Labels: l1, Histogram: hAt(1, 1, 1, 0, 0, 0)})
	_ = st.Ingest(store.Sample{Timestamp: t0, Labels: l2, Histogram: hAt(1, 1, 1, 0, 0, 0)})
	_ = st.Ingest(store.Sample{Timestamp: t0.Add(time.Minute), Labels: l1, Histogram: hAt(2, 3, 2, 0, 0, 0)})

	all := st.Latest(nil)
	if len(all) != 2 {
		t.Fatalf("want 2 series, got %d", len(all))
	}
	one := st.Latest(store.Selector{{Name: "service", Value: "a"}})
	if len(one) != 1 || one[0].Histogram.TotalCount != 2 {
		t.Fatalf("selector latest total wrong: %+v", one)
	}
	none := st.Latest(store.Selector{{Name: "service", Value: "zzz"}})
	if len(none) != 0 {
		t.Fatalf("unmatched selector must return empty")
	}
}

func TestWAL_PersistAndReplay(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "wal.jsonl")

	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	func() {
		st, err := store.New(path)
		if err != nil {
			t.Fatal(err)
		}
		defer st.Close()
		if err := st.Ingest(store.Sample{Timestamp: t0, Labels: labels(), Histogram: hAt(2, 2, 1, 1, 0, 0)}); err != nil {
			t.Fatal(err)
		}
		if err := st.Ingest(store.Sample{Timestamp: t0.Add(time.Minute), Labels: labels(), Histogram: hAt(5, 8, 2, 1, 1, 1)}); err != nil {
			t.Fatal(err)
		}
	}()

	st2, err := store.New(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	latest := st2.Latest(nil)
	if len(latest) != 1 {
		t.Fatalf("after replay want 1 series, got %d", len(latest))
	}
	if latest[0].Histogram.TotalCount != 5 {
		t.Fatalf("after replay total = %d, want 5", latest[0].Histogram.TotalCount)
	}
	if st2.CountSamples(labels()) != 2 {
		t.Fatal("both samples must be replayed")
	}
}

func TestIncrease_WindowDelta(t *testing.T) {
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	samples := []store.Sample{
		{Timestamp: t0, Labels: labels(), Histogram: hAt(2, 2, 1, 1, 0, 0)},
		{Timestamp: t0.Add(time.Minute), Labels: labels(), Histogram: hAt(5, 8, 2, 1, 1, 1)},
	}
	delta := store.Increase(samples)
	if delta == nil {
		t.Fatal("delta must not be nil")
	}
	if delta.TotalCount != 3 {
		t.Fatalf("delta total = %d, want 3", delta.TotalCount)
	}
	// incrementals of delta: (2-1),(1-1),(1-0),(1-0) => cumulative 1,1,2,3
	want := []uint64{1, 1, 2, 3}
	for i, w := range want {
		if delta.Buckets[i].CumulativeCount != w {
			t.Fatalf("delta bucket %d = %d, want %d", i, delta.Buckets[i].CumulativeCount, w)
		}
	}
	// The delta must itself be a valid cumulative histogram.
	if err := delta.Validate(); err != nil {
		t.Fatalf("delta invalid: %v", err)
	}
}

func TestIncrease_SingleSampleIsNil(t *testing.T) {
	if store.Increase([]store.Sample{{Timestamp: time.Now()}}) != nil {
		t.Fatal("increase with <2 samples must be nil")
	}
}
