package store

import (
	"errors"
	"path/filepath"
	"sync"
	"testing"

	"metricrollup/internal/model"
	"metricrollup/internal/rollup"
)

var testLabels = map[string]string{"host": "h1"}

type p struct {
	ts int64
	v  float64
}

func mkSamples(metric string, labels map[string]string, pts ...p) []model.Sample {
	out := make([]model.Sample, 0, len(pts))
	for _, pt := range pts {
		out = append(out, model.Sample{Metric: metric, Labels: labels, Ts: pt.ts, Value: pt.v})
	}
	return out
}

func TestIngestAndQueryLayers(t *testing.T) {
	st := New()
	// Two samples in the same minute and hour.
	st.Ingest(mkSamples("m", testLabels, p{2*3600 + 10, 10}, p{2*3600 + 50, 20}))

	minutes, err := st.Query("m", testLabels, 2*3600, 2*3600+60, LayerMinute)
	if err != nil {
		t.Fatal(err)
	}
	if len(minutes) != 1 {
		t.Fatalf("want 1 minute bucket, got %d", len(minutes))
	}
	if minutes[0].Count != 2 || *minutes[0].Sum != 30 ||
		*minutes[0].Min != 10 || *minutes[0].Max != 20 {
		t.Fatalf("unexpected minute bucket: %+v", minutes[0])
	}
	if minutes[0].Mean == nil || *minutes[0].Mean != 15 {
		t.Fatalf("mean wrong: %+v", minutes[0])
	}

	hours, err := st.Query("m", testLabels, 2*3600, 3*3600, LayerHour)
	if err != nil {
		t.Fatal(err)
	}
	if len(hours) != 1 || hours[0].Count != 2 || *hours[0].Sum != 30 {
		t.Fatalf("unexpected hour bucket: %+v", hours)
	}
}

func TestCrossLayerBoundaries(t *testing.T) {
	st := New()
	// 02:00:59 -> minute :00, hour 02
	// 02:01:00 -> minute :01, hour 02
	// 02:59:59 -> minute :59, hour 02
	// 03:00:00 -> minute :00, hour 03
	st.Ingest(mkSamples("m", testLabels,
		p{2*3600 + 59, 1},
		p{2*3600 + 60, 2},
		p{3*3600 - 1, 3},
		p{3 * 3600, 4},
	))

	minutes, _ := st.Query("m", testLabels, 2*3600, 2*3600+120, LayerMinute)
	if len(minutes) != 2 {
		t.Fatalf("want 2 minute buckets, got %d", len(minutes))
	}
	if minutes[0].Count != 1 || *minutes[0].Sum != 1 {
		t.Fatalf("minute 02:00 wrong: %+v", minutes[0])
	}
	if minutes[1].Count != 1 || *minutes[1].Sum != 2 {
		t.Fatalf("minute 02:01 wrong: %+v", minutes[1])
	}

	hours, _ := st.Query("m", testLabels, 2*3600, 4*3600, LayerHour)
	if len(hours) != 2 {
		t.Fatalf("want 2 hour buckets, got %d", len(hours))
	}
	if hours[0].Count != 3 || *hours[0].Sum != 6 { // 1 + 2 + 3
		t.Fatalf("hour 02 wrong: %+v", hours[0])
	}
	if hours[1].Count != 1 || *hours[1].Sum != 4 {
		t.Fatalf("hour 03 wrong: %+v", hours[1])
	}
}

func TestLateArrivalsAreOrderIndependent(t *testing.T) {
	pts := []p{{100, 1}, {101, 2}, {102, 3}, {130, 4}, {4000, 5}}

	forward := New()
	forward.Ingest(mkSamples("m", testLabels, pts...))

	backward := New()
	for i := len(pts) - 1; i >= 0; i-- {
		backward.Ingest(mkSamples("m", testLabels, pts[i]))
	}

	for _, layer := range []Layer{LayerRaw, LayerMinute, LayerHour} {
		a, err := forward.Query("m", testLabels, 0, 4200, layer)
		if err != nil {
			t.Fatal(err)
		}
		b, err := backward.Query("m", testLabels, 0, 4200, layer)
		if err != nil {
			t.Fatal(err)
		}
		if len(a) != len(b) {
			t.Fatalf("%s: bucket count differs", layer)
		}
		for i := range a {
			if a[i].Count != b[i].Count {
				t.Fatalf("%s bucket %d count: %d != %d", layer, a[i].Start, a[i].Count, b[i].Count)
			}
			if (a[i].Sum == nil) != (b[i].Sum == nil) {
				t.Fatalf("%s bucket %d nil-ness differs", layer, a[i].Start)
			}
			if a[i].Sum != nil && (*a[i].Sum != *b[i].Sum || *a[i].Min != *b[i].Min || *a[i].Max != *b[i].Max) {
				t.Fatalf("%s bucket %d differs: %+v vs %+v", layer, a[i].Start, a[i], b[i])
			}
		}
	}
}

func TestEmptyBucketsIncluded(t *testing.T) {
	st := New()
	// One sample in minute 0 and minute 3; minute 1 and 2 are gaps.
	st.Ingest(mkSamples("m", testLabels, p{5, 10}, p{185, 20}))

	got, err := st.Query("m", testLabels, 0, 240, LayerMinute)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 4 {
		t.Fatalf("want 4 buckets incl. gaps, got %d", len(got))
	}
	wantCount := []int64{1, 0, 0, 1}
	for i, b := range got {
		if b.Count != wantCount[i] {
			t.Errorf("bucket %d count = %d, want %d", i, b.Count, wantCount[i])
		}
		if b.Count == 0 && (b.Sum != nil || b.Min != nil || b.Max != nil || b.Mean != nil) {
			t.Errorf("empty bucket %d must have nil numeric fields: %+v", b.Start, b)
		}
	}
}

func TestQueryRangeAlignment(t *testing.T) {
	st := New()
	st.Ingest(mkSamples("m", testLabels, p{90, 1})) // minute 60

	// Unaligned start/end get floored/ceiled to boundaries.
	got, err := st.Query("m", testLabels, 65, 119, LayerMinute)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Start != 60 || got[0].Count != 1 {
		t.Fatalf("alignment wrong: %+v", got)
	}
}

func TestQueryValidation(t *testing.T) {
	st := New()
	if _, err := st.Query("m", nil, 100, 100, LayerMinute); err == nil {
		t.Error("want error for empty range")
	}
	if _, err := st.Query("m", nil, 0, 10, Layer("bogus")); err == nil {
		t.Error("want error for unknown layer")
	}
	if _, err := st.Query("missing", nil, 0, 10, LayerMinute); !errors.Is(err, ErrNotFound) {
		t.Errorf("want ErrNotFound, got %v", err)
	}
}

func TestCorrectPropagates(t *testing.T) {
	st := New()
	// Fill minute 02:00 and hour 02 with samples spread across the hour.
	st.Ingest(mkSamples("m", testLabels,
		p{2*3600 + 0, 10},  // 02:00:00
		p{2*3600 + 30, 20}, // 02:00:30
		p{2*3600 + 59, 30}, // 02:00:59
		p{3*3600 - 1, 99},  // 02:59:59, same hour, minute :59
	))

	// Correct 02:00:00 from 10 to 1000.
	if err := st.Correct("m", testLabels, 2*3600, 1000); err != nil {
		t.Fatal(err)
	}

	// Raw second replaced wholesale.
	raw, _ := st.Query("m", testLabels, 2*3600, 2*3600+1, LayerRaw)
	if raw[0].Count != 1 || *raw[0].Sum != 1000 {
		t.Fatalf("raw replace wrong: %+v", raw[0])
	}

	// Minute 02:00 rebuilt from raw: 1000 + 20 + 30.
	minutes, _ := st.Query("m", testLabels, 2*3600, 2*3600+60, LayerMinute)
	if minutes[0].Count != 3 || *minutes[0].Sum != 1050 ||
		*minutes[0].Min != 20 || *minutes[0].Max != 1000 {
		t.Fatalf("minute not rebuilt: %+v", minutes[0])
	}

	// Hour rebuilt from minutes: 1000,20,30 plus 99 at 02:59:59.
	hours, _ := st.Query("m", testLabels, 2*3600, 3*3600, LayerHour)
	if hours[0].Count != 4 || *hours[0].Sum != 1149 ||
		*hours[0].Min != 20 || *hours[0].Max != 1000 {
		t.Fatalf("hour not rebuilt: %+v", hours[0])
	}
}

func TestCorrectUnknownSeriesAndSecond(t *testing.T) {
	st := New()
	st.Ingest(mkSamples("m", testLabels, p{10, 1}))

	if err := st.Correct("other", nil, 10, 2); !errors.Is(err, ErrNotFound) {
		t.Errorf("unknown series: want ErrNotFound, got %v", err)
	}
	if err := st.Correct("m", testLabels, 11, 2); !errors.Is(err, ErrRawUnavailable) {
		t.Errorf("empty second: want ErrRawUnavailable, got %v", err)
	}
}

func TestCorrectAfterPruneRejected(t *testing.T) {
	st := New()
	st.Ingest(mkSamples("m", testLabels, p{0, 1}, p{3599, 2}, p{3600, 3}))

	removed := st.PruneRaw(3600)
	if removed != 2 {
		t.Fatalf("want 2 raw samples pruned, got %d", removed)
	}

	// Hour layer still serves the pre-prune aggregate.
	hours, _ := st.Query("m", testLabels, 0, 7200, LayerHour)
	if hours[0].Count != 2 || hours[1].Count != 1 {
		t.Fatalf("hour layer altered by prune: %+v", hours)
	}

	// Revision of pruned history is impossible.
	if err := st.Correct("m", testLabels, 0, 99); !errors.Is(err, ErrRawUnavailable) {
		t.Fatalf("want ErrRawUnavailable after prune, got %v", err)
	}
	// Revision in retained raw data still works.
	if err := st.Correct("m", testLabels, 3600, 30); err != nil {
		t.Fatalf("correct retained second: %v", err)
	}
}

func TestSeriesIsolation(t *testing.T) {
	st := New()
	st.Ingest([]model.Sample{
		{Metric: "m", Labels: map[string]string{"host": "a"}, Ts: 0, Value: 1},
		{Metric: "m", Labels: map[string]string{"host": "b"}, Ts: 0, Value: 2},
	})
	a, err := st.Query("m", map[string]string{"host": "a"}, 0, 60, LayerMinute)
	if err != nil {
		t.Fatal(err)
	}
	if a[0].Count != 1 || *a[0].Sum != 1 {
		t.Fatalf("series leaked across labels: %+v", a[0])
	}
	infos := st.ListSeries()
	if len(infos) != 2 {
		t.Fatalf("want 2 series, got %d", len(infos))
	}
}

func TestSnapshotRoundTrip(t *testing.T) {
	st := New()
	st.Ingest(mkSamples("m", testLabels,
		p{0, 1}, p{59, 2}, p{3600, 3}, p{7199, -4},
	))

	path := filepath.Join(t.TempDir(), "snap.json")
	if err := st.Save(path); err != nil {
		t.Fatal(err)
	}

	restored := New()
	if err := restored.Load(path); err != nil {
		t.Fatal(err)
	}
	hours, err := restored.Query("m", testLabels, 0, 7200, LayerHour)
	if err != nil {
		t.Fatal(err)
	}
	if len(hours) != 2 || hours[0].Count != 2 || hours[1].Count != 2 {
		t.Fatalf("snapshot round-trip lost data: %+v", hours)
	}
	if *hours[1].Min != -4 || *hours[1].Sum != -1 {
		t.Fatalf("hour 1 aggregates wrong: %+v", hours[1])
	}
}

func TestLoadMissingFileIsEmpty(t *testing.T) {
	st := New()
	if err := st.Load(filepath.Join(t.TempDir(), "absent.json")); err != nil {
		t.Fatalf("missing file should be no-op: %v", err)
	}
}

func TestMultipleSamplesSameSecond(t *testing.T) {
	st := New()
	st.Ingest(mkSamples("m", testLabels, p{42, 5}, p{42, 7}))

	raw, _ := st.Query("m", testLabels, 42, 43, LayerRaw)
	if raw[0].Count != 2 || *raw[0].Sum != 12 || *raw[0].Min != 5 || *raw[0].Max != 7 {
		t.Fatalf("same-second merge wrong: %+v", raw[0])
	}
	// Correct replaces every value at that second.
	if err := st.Correct("m", testLabels, 42, 9); err != nil {
		t.Fatal(err)
	}
	raw, _ = st.Query("m", testLabels, 42, 43, LayerRaw)
	if raw[0].Count != 1 || *raw[0].Sum != 9 {
		t.Fatalf("correct should replace all values: %+v", raw[0])
	}
}

// Guard: confirm the widths divide as the rebuild loops assume.
func TestLayerWidthsDivide(t *testing.T) {
	if rollup.Hour%rollup.Minute != 0 || rollup.Minute%rollup.Second != 0 {
		t.Fatal("retention widths must nest evenly")
	}
}

func TestConcurrentIngestQuery(t *testing.T) {
	st := New()
	const writers = 8
	const perWriter = 200

	var wg sync.WaitGroup
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			batch := make([]model.Sample, 0, perWriter)
			for i := 0; i < perWriter; i++ {
				// Spread writers across seconds of one hour so all share
				// (and concurrently merge into) the same hour bucket.
				batch = append(batch, model.Sample{
					Metric: "m", Labels: testLabels,
					Ts:    int64(w*perWriter+i) % 3600,
					Value: float64(i),
				})
			}
			st.Ingest(batch)
		}(w)
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	for {
		select {
		case <-done:
			hours, err := st.Query("m", testLabels, 0, 3600, LayerHour)
			if err != nil {
				t.Error(err)
			}
			var total int64
			for _, b := range hours {
				total += b.Count
			}
			if want := int64(writers * perWriter); total != want {
				t.Fatalf("lost samples under concurrency: %d != %d", total, want)
			}
			return
		default:
			_, _ = st.Query("m", testLabels, 0, 3600, LayerMinute)
		}
	}
}
