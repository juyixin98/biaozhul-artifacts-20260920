package service_test

import (
	"context"
	"fmt"
	"os"
	"sync/atomic"
	"testing"

	"twap/internal/service"
	"twap/internal/storage"
	"twap/internal/twap"
)

const (
	us           = int64(1)
	ms           = 1_000 * us
	second       = 1_000 * ms
	windowMicros = int64(60 * second)
)

type fakeClock struct{ t atomic.Int64 }

func (c *fakeClock) get() int64  { return c.t.Load() }
func (c *fakeClock) set(v int64) { c.t.Store(v) }

type harness struct {
	svc *service.Service
	db  *storage.DB
	clk *fakeClock
	ctx context.Context
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		url = "postgres://twap_p015_a:twap_p015_a_pw@localhost:5432/twap_p015_a_svc?sslmode=disable"
	}
	ctx := context.Background()
	db, err := storage.New(ctx, url)
	if err != nil {
		t.Skipf("postgresql not available (%v); set TEST_DATABASE_URL to run integration tests", err)
	}
	if err := db.EnsureSchema(ctx); err != nil {
		t.Fatalf("schema: %v", err)
	}
	if _, err := db.Pool.Exec(ctx, `TRUNCATE samples, window_versions`); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	clk := &fakeClock{}
	key := []byte("integration-test-signing-key-32b!!")
	svc := service.New(db, service.Config{
		WindowMicros:     windowMicros,
		MaxLateMicros:    5 * 60 * second,
		StaleAfterMicros: 30 * second,
		SigningKey:       key,
	}, clk.get)
	h := &harness{svc: svc, db: db, clk: clk, ctx: ctx}
	t.Cleanup(func() {
		_, _ = db.Pool.Exec(ctx, `TRUNCATE samples, window_versions`)
		db.Close()
	})
	return h
}

func mustIngest(t *testing.T, h *harness, ts, price int64, src string) *service.IngestResult {
	t.Helper()
	r, err := h.svc.Ingest(h.ctx, twap.Sample{TS: ts, Price: price, Source: src})
	if err != nil {
		t.Fatalf("ingest ts=%d price=%d src=%s: %v", ts, price, src, err)
	}
	return r
}

func windowStart(i int) int64 { return int64(i) * windowMicros }

func TestLateArrivalRecomputesAndVersions(t *testing.T) {
	h := newHarness(t)
	b0s := windowStart(10) // fixed absolute bucket, avoids alignment luck

	// t=0 of bucket: source a reports 100. Clock at window end: bucket
	// closes and materializes v1.
	h.clk.set(b0s + windowMicros)
	r := mustIngest(t, h, b0s, 100, "a")
	if len(r.Recompute) != 1 {
		t.Fatalf("recomputed windows = %d, want 1", len(r.Recompute))
	}
	v1 := r.Recompute[0]
	if v1.Version != 1 || !v1.Created {
		t.Fatalf("first materialization = %+v, want version 1 created", v1)
	}

	// Read v1: coverage full, twap 100.
	view, err := h.svc.ReadWindow(h.ctx, b0s, true)
	if err != nil {
		t.Fatal(err)
	}
	if view.Version != 1 || view.TWAP == nil || *view.TWAP != 100 {
		t.Fatalf("v1 view = %v version %d", view.TWAP, view.Version)
	}
	if view.SignatureOK == nil || !*view.SignatureOK {
		t.Fatal("v1 signature must verify")
	}

	// A late sample arrives 2 minutes after close (within the 5-minute
	// allowance), asserting a different price at t=30s. It must
	// recompute bucket 0 and publish v2. (The clock also closes bucket
	// 1, which is expected to materialize on its own; we assert on the
	// entry for bucket 0 only.)
	lateTS := b0s + 30*second
	h.clk.set(b0s + windowMicros + 2*60*second)
	r = mustIngest(t, h, lateTS, 200, "a")
	var b0 *service.RecomputedWindow
	for i := range r.Recompute {
		if r.Recompute[i].WindowStart == b0s {
			b0 = &r.Recompute[i]
		}
	}
	if b0 == nil || b0.Version != 2 {
		t.Fatalf("bucket0 late recompute = %+v, want v2", r.Recompute)
	}
	// (100*30 + 200*30)/60 = 150
	view2, err := h.svc.ReadWindow(h.ctx, b0s, true)
	if err != nil {
		t.Fatal(err)
	}
	if view2.Version != 2 || view2.TWAP == nil || *view2.TWAP != 150 {
		t.Fatalf("v2 = twap %v version %d, want 150/2", view2.TWAP, view2.Version)
	}
	if !*view2.SignatureOK {
		t.Fatal("v2 signature must verify")
	}

	// v1 remains readable historically.
	old, err := h.svc.ReadVersion(h.ctx, b0s, 1, false)
	if err != nil {
		t.Fatal(err)
	}
	if old.TWAP == nil || *old.TWAP != 100 {
		t.Fatalf("historical v1 twap = %v, want 100", old.TWAP)
	}
}

func TestIdempotentNoNewVersion(t *testing.T) {
	h := newHarness(t)
	b0s := windowStart(20)
	h.clk.set(b0s + windowMicros)
	mustIngest(t, h, b0s, 100, "a")

	// Re-send identical observation within late window (30s later, while
	// the next bucket is still open): stored value unchanged, so no new
	// version for bucket 0.
	h.clk.set(b0s + windowMicros + 30*second)
	r := mustIngest(t, h, b0s, 100, "a")
	if r.Changed {
		t.Fatal("identical re-ingest must report changed=false")
	}
	for _, rc := range r.Recompute {
		if rc.WindowStart == b0s {
			t.Fatalf("identical re-ingest created a bucket0 version: %+v", rc)
		}
	}
}

func TestRejectAfterFreeze(t *testing.T) {
	h := newHarness(t)
	b0s := windowStart(30)
	// Clock beyond the 5-minute late cutoff.
	h.clk.set(b0s + windowMicros + 5*60*second + 1)
	_, err := h.svc.Ingest(h.ctx, twap.Sample{TS: b0s, Price: 1, Source: "a"})
	if err == nil {
		t.Fatal("sample past cutoff was accepted")
	}
}

func TestRejectFutureSample(t *testing.T) {
	h := newHarness(t)
	h.clk.set(windowStart(40))
	_, err := h.svc.Ingest(h.ctx, twap.Sample{
		TS: windowStart(40) + 10*second, Price: 1, Source: "a",
	})
	if err == nil {
		t.Fatal("future sample was accepted")
	}
}

func TestIncrementalEqualsFullRecompute(t *testing.T) {
	h := newHarness(t)
	base := windowStart(100)

	// Ingest an irregular, multi-source stream across three buckets,
	// advancing the clock so each ingest goes through the incremental
	// materialization path.
	samples := []twap.Sample{
		{TS: base + 5*second, Price: 100, Source: "a"},
		{TS: base + 12*second, Price: 102, Source: "b"},
		{TS: base + 55*second, Price: 130, Source: "a"},
		{TS: base + windowMicros + 1*second, Price: 200, Source: "a"},
		{TS: base + windowMicros + 40*second, Price: 210, Source: "b"},
		{TS: base + 2*windowMicros + 20*second, Price: 300, Source: "a"},
		// anchor sample strictly inside bucket 3
		{TS: base + 3*windowMicros - 1*second, Price: 999, Source: "a"},
	}

	// Clock at the end of bucket 3 minus 1s: buckets 0 and 1 are closed
	// and materialize incrementally; bucket 2 is still OPEN, so its read
	// is live (no stored version, no signature) and the full sweep does
	// not touch it.
	h.clk.set(base + 3*windowMicros - 1)
	for _, s := range samples {
		mustIngest(t, h, s.TS, s.Price, s.Source)
	}

	// Snapshot the incremental/live results.
	incViews := map[int64]*service.WindowView{}
	for i := int64(0); i < 3; i++ {
		ws := base + i*windowMicros
		v, err := h.svc.ReadWindow(h.ctx, ws, i < 2)
		if err != nil {
			t.Fatal(err)
		}
		incViews[ws] = v
		if i < 2 {
			if v.SignatureOK == nil || !*v.SignatureOK {
				t.Fatalf("closed window %d signature invalid", i)
			}
		} else {
			if !v.Live {
				t.Fatalf("window 2 must be a live open read")
			}
		}
	}

	// Full recompute must find zero diffs on the closed windows (the
	// incremental path already converged to the same content hashes).
	diffs, checked, err := h.svc.FullRecompute(h.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if checked != 2 {
		t.Fatalf("checked = %d, want 2 closed windows", checked)
	}
	if len(diffs) != 0 {
		t.Fatalf("full recompute diverged from incremental: %+v", diffs)
	}

	// Closed-window versions and hashes are unchanged; every window's
	// stored/live result matches the raw-scan range reference.
	for i := int64(0); i < 3; i++ {
		ws := base + i*windowMicros
		v, err := h.svc.ReadWindow(h.ctx, ws, false)
		if err != nil {
			t.Fatal(err)
		}
		if v.Version != incViews[ws].Version || v.InputHash != incViews[ws].InputHash {
			t.Fatalf("window %d changed after full recompute: %d/%s vs %d/%s",
				i, v.Version, v.InputHash,
				incViews[ws].Version, incViews[ws].InputHash)
		}
		rng, err := h.svc.ReadRange(h.ctx, ws, ws+windowMicros)
		if err != nil {
			t.Fatal(err)
		}
		if rng.InputHash != v.InputHash {
			t.Fatalf("window %d range hash %s != stored %s", i, rng.InputHash, v.InputHash)
		}
		if (rng.TWAP == nil) != (v.TWAP == nil) ||
			(rng.TWAP != nil && *rng.TWAP != *v.TWAP) {
			t.Fatalf("window %d range twap %v != stored %v", i, rng.TWAP, v.TWAP)
		}
		if rng.CoveredMicros != v.CoveredMicros {
			t.Fatalf("window %d range covered %d != stored %d",
				i, rng.CoveredMicros, v.CoveredMicros)
		}
	}
}

func TestLateAnchorAffectsNextBucket(t *testing.T) {
	h := newHarness(t)
	base := windowStart(150)

	// A pre-bucket anchor sample, ingested while bucket 1 is open.
	h.clk.set(base + 10*second)
	mustIngest(t, h, base-2*second, 100, "a")

	// Bucket 1 closes; the read on-demand-materializes v1 = 100.
	h.clk.set(base + windowMicros + 5*second)
	v1, err := h.svc.ReadWindow(h.ctx, base, true)
	if err != nil {
		t.Fatal(err)
	}
	if v1.Version != 1 || v1.TWAP == nil || *v1.TWAP != 100 {
		t.Fatalf("bucket1 v1 = %v/%d, want 100/1", v1.TWAP, v1.Version)
	}

	// Late correction of the SAME anchor timestamp to price 50.
	mustIngest(t, h, base-2*second, 50, "a")
	v2, err := h.svc.ReadWindow(h.ctx, base, true)
	if err != nil {
		t.Fatal(err)
	}
	if v2.Version != 2 || v2.TWAP == nil || *v2.TWAP != 50 {
		t.Fatalf("bucket1 v2 = %v/%d, want 50/2", v2.TWAP, v2.Version)
	}
	if v2.SignatureOK == nil || !*v2.SignatureOK {
		t.Fatal("bucket1 v2 signature must verify")
	}

	// v1 is still readable historically.
	old, err := h.svc.ReadVersion(h.ctx, base, 1, false)
	if err != nil {
		t.Fatal(err)
	}
	if old.TWAP == nil || *old.TWAP != 100 {
		t.Fatalf("historical v1 = %v, want 100", old.TWAP)
	}

	// The still-open bucket 2 sees the corrected anchor 50.
	b2, err := h.svc.ReadWindow(h.ctx, base+windowMicros, false)
	if err != nil {
		t.Fatal(err)
	}
	if !b2.Live || b2.TWAP == nil || *b2.TWAP != 50 {
		t.Fatalf("open bucket2 anchor = %v live=%v, want 50 live", b2.TWAP, b2.Live)
	}
}

func TestInsufficientCoverageAndStaleOnRead(t *testing.T) {
	h := newHarness(t)
	base := windowStart(200)
	// One sample at 40s into a closed bucket: prefix [0,40s) uncovered,
	// and the carrying sample is 20s old at window end (< 30s stale
	// threshold -> not stale).
	h.clk.set(base + windowMicros)
	mustIngest(t, h, base+40*second, 100, "a")
	v, err := h.svc.ReadWindow(h.ctx, base, false)
	if err != nil {
		t.Fatal(err)
	}
	if v.CoveredMicros != 20*second {
		t.Fatalf("covered = %d, want 20s", v.CoveredMicros)
	}
	if v.Coverage < 0.3332 || v.Coverage > 0.3334 {
		t.Fatalf("coverage = %v, want 1/3", v.Coverage)
	}
	if v.Stale {
		t.Fatal("20s-old carrier must not be stale (threshold 30s)")
	}
	if v.HasAnchor {
		t.Fatal("no pre-window anchor should exist")
	}
}

func TestTamperedStoredSignatureFails(t *testing.T) {
	h := newHarness(t)
	base := windowStart(250)
	h.clk.set(base + windowMicros)
	mustIngest(t, h, base, 100, "a")
	before, err := h.svc.ReadWindow(h.ctx, base, true)
	if err != nil {
		t.Fatal(err)
	}
	// Tamper with the stored twap directly in the database.
	_, err = h.db.Pool.Exec(h.ctx,
		`UPDATE window_versions SET twap_num='999' WHERE window_start=$1`,
		base)
	if err != nil {
		t.Fatal(err)
	}
	after, err := h.svc.ReadWindow(h.ctx, base, true)
	if err != nil {
		t.Fatal(err)
	}
	if after.SignatureOK == nil || *after.SignatureOK {
		exact := "<nil>"
		if after.TWAPExact != nil {
			exact = *after.TWAPExact
		}
		t.Fatalf("tampered row verified; before sig=%s after sig=%s num=%s",
			before.Signature, after.Signature, exact)
	}
}

func TestConcurrentIngestsSameWindow(t *testing.T) {
	h := newHarness(t)
	base := windowStart(300)
	h.clk.set(base + windowMicros + 30*second)
	errs := make(chan error, 20)
	done := make(chan struct{}, 20)
	for i := 0; i < 20; i++ {
		i := i
		go func() {
			_, err := h.svc.Ingest(h.ctx, twap.Sample{
				TS:     base + int64(i)*second,
				Price:  int64(100 + i),
				Source: fmt.Sprintf("src%02d", i),
			})
			if err != nil {
				errs <- err
			}
			done <- struct{}{}
		}()
	}
	for i := 0; i < 20; i++ {
		<-done
	}
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent ingest: %v", err)
	}
	v, err := h.svc.ReadWindow(h.ctx, base, true)
	if err != nil {
		t.Fatal(err)
	}
	if v.SignatureOK == nil || !*v.SignatureOK {
		t.Fatal("final concurrent version signature invalid")
	}
	// Every distinct in-window timestamp from src00..src19 present.
	if v.SamplesUsed < 20 {
		t.Fatalf("samples used = %d, want >= 20", v.SamplesUsed)
	}
}
