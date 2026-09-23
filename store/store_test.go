package store

import (
	"errors"
	"testing"

	"counterreset/counter"
)

func samples(s ...counter.Sample) []counter.Sample { return s }

func TestIngestAndGet(t *testing.T) {
	dir := t.TempDir()
	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	n, dup, err := st.Ingest("m", map[string]string{"h": "a"}, samples(
		counter.Sample{T: 2, Value: 2}, counter.Sample{T: 1, Value: 1}))
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 || dup != 0 {
		t.Errorf("new=%d dup=%d, want 2/0", n, dup)
	}
	got, ok := st.Get("m", map[string]string{"h": "a"})
	if !ok {
		t.Fatal("series missing")
	}
	if got.Samples[0].T != 1 || got.Samples[1].T != 2 {
		t.Errorf("samples not sorted: %+v", got.Samples)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestWALReplay(t *testing.T) {
	dir := t.TempDir()
	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	labels := map[string]string{"h": "a"}
	if _, _, err := st.Ingest("m", labels, samples(
		counter.Sample{T: 0, Value: 5}, counter.Sample{T: 10, Value: 9})); err != nil {
		t.Fatal(err)
	}
	// Second ingest, including an exact duplicate.
	if _, _, err := st.Ingest("m", labels, samples(
		counter.Sample{T: 10, Value: 9}, counter.Sample{T: 20, Value: 13})); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	got, ok := st2.Get("m", labels)
	if !ok {
		t.Fatal("series did not survive replay")
	}
	if len(got.Samples) != 3 {
		t.Fatalf("replayed samples = %d, want 3 (dup collapsed)", len(got.Samples))
	}
	if got.Samples[2].Value != 13 {
		t.Errorf("last value = %v, want 13", got.Samples[2].Value)
	}
}

func TestDuplicateSameValueIdempotent(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	defer st.Close()
	if _, _, err := st.Ingest("m", nil, samples(counter.Sample{T: 1, Value: 1})); err != nil {
		t.Fatal(err)
	}
	n, dup, err := st.Ingest("m", nil, samples(counter.Sample{T: 1, Value: 1}))
	if err != nil {
		t.Fatalf("same-value duplicate should be accepted: %v", err)
	}
	if n != 0 || dup != 1 {
		t.Errorf("new=%d dup=%d, want 0/1", n, dup)
	}
}

func TestDuplicateDifferentValueConflict(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	defer st.Close()
	if _, _, err := st.Ingest("m", nil, samples(counter.Sample{T: 1, Value: 1})); err != nil {
		t.Fatal(err)
	}
	_, _, err := st.Ingest("m", nil, samples(counter.Sample{T: 1, Value: 2}))
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("want ErrConflict, got %v", err)
	}
	// Within-request clash.
	_, _, err = st.Ingest("m2", nil, samples(
		counter.Sample{T: 1, Value: 1}, counter.Sample{T: 1, Value: 2}))
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("want ErrConflict for in-request clash, got %v", err)
	}
}

func TestNegativeRejected(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	defer st.Close()
	if _, _, err := st.Ingest("m", nil, samples(counter.Sample{T: 1, Value: -1})); err == nil {
		t.Fatal("negative value must be rejected")
	}
	if got := len(st.List()); got != 0 {
		t.Fatalf("failed ingest must not create a series, got %d", got)
	}
}

func TestLabelIdentity(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	defer st.Close()
	if _, _, err := st.Ingest("m", map[string]string{"a": "1", "b": "2"},
		samples(counter.Sample{T: 0, Value: 1})); err != nil {
		t.Fatal(err)
	}
	// Same labels, different insertion order: identical series.
	if _, _, err := st.Ingest("m", map[string]string{"b": "2", "a": "1"},
		samples(counter.Sample{T: 10, Value: 2})); err != nil {
		t.Fatal(err)
	}
	if got := len(st.List()); got != 1 {
		t.Fatalf("label order must not create a new series, got %d", got)
	}
	// A different label value is a different series.
	if _, _, err := st.Ingest("m", map[string]string{"a": "1", "b": "3"},
		samples(counter.Sample{T: 0, Value: 1})); err != nil {
		t.Fatal(err)
	}
	if got := len(st.List()); got != 2 {
		t.Fatalf("want 2 distinct series, got %d", got)
	}
}

func TestReset(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	if _, _, err := st.Ingest("m", nil, samples(counter.Sample{T: 0, Value: 1})); err != nil {
		t.Fatal(err)
	}
	if err := st.Reset(); err != nil {
		t.Fatal(err)
	}
	if got := len(st.List()); got != 0 {
		t.Fatalf("after reset want 0 series, got %d", got)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	// Replay must honor the reset marker.
	st2, _ := Open(dir)
	defer st2.Close()
	if got := len(st2.List()); got != 0 {
		t.Fatalf("after replay post-reset want 0 series, got %d", got)
	}
}
