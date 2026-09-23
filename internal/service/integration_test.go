package service_test

import (
	"context"
	"os"
	"sync"
	"testing"
	"time"

	"twap-service/internal/domain"
	"twap-service/internal/service"
	"twap-service/internal/store"
)

// fixedClock is a controllable time source for deterministic tests.
type fixedClock struct {
	mu sync.RWMutex
	t  time.Time
}

func newFixedClock(t time.Time) *fixedClock { return &fixedClock{t: t} }
func (c *fixedClock) Now() time.Time {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.t
}
func (c *fixedClock) Set(t time.Time) {
	c.mu.Lock()
	c.t = t
	c.mu.Unlock()
}
func (c *fixedClock) Add(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
	return c.t
}

func testDSN() string {
	if v := os.Getenv("TWAP_TEST_DSN"); v != "" {
		return v
	}
	return "postgres://twap:twappw@127.0.0.1:5432/twap?sslmode=disable&statement_cache_mode=describe"
}

// setupIntegration opens the store against a real PostgreSQL instance and
// truncates all data. Tests skip when TWAP_TEST_DSN_SKIP is set to "1".
func setupIntegration(t *testing.T, mode service.ConflictMode) (*service.Service, *store.Store, *fixedClock) {
	t.Helper()
	if os.Getenv("TWAP_TEST_SKIP_DB") == "1" {
		t.Skip("TWAP_TEST_SKIP_DB=1")
	}
	ctx := context.Background()
	st, err := store.New(ctx, testDSN())
	if err != nil {
		t.Skipf("postgresql not available (%v); set TWAP_TEST_DSN or TWAP_TEST_SKIP_DB=1", err)
	}
	t.Cleanup(st.Close)
	// Wipe only data tables; schema/migrations stay intact (schema.sql is
	// idempotent and is applied by store.New on each fresh connection).
	for _, tbl := range []string{"window_versions", "used_nonces", "samples", "sources"} {
		if _, err := st.Pool().Exec(ctx, "TRUNCATE "+tbl+" CASCADE"); err != nil {
			t.Fatalf("truncate %s: %v", tbl, err)
		}
	}

	clock := newFixedClock(time.Date(2026, 1, 1, 0, 10, 0, 0, time.UTC))
	cfg := domain.Config{
		WindowSec: 60, LateToleranceSec: 300,
		FutureGraceSec: 2, StaleHorizonSec: 120,
	}
	svc := service.New(st, cfg, mode, clock.Now)

	for _, s := range []struct {
		name string
		prio int
	}{
		{"venueA", 10}, {"venueB", 5}, {"venueC", 0},
	} {
		if err := svc.RegisterSource(ctx, s.name, s.prio,
			"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="); err != nil {
			t.Fatal(err)
		}
	}
	return svc, st, clock
}

// ingestAt sends a sample and moves the clock to just after its timestamp,
// simulating realistic arrival order.
func ingestAt(t *testing.T, svc *service.Service, clock *fixedClock,
	symbol string, ts time.Time, price int64, src string) *service.IngestResult {
	t.Helper()
	clock.Set(ts.Add(time.Second))
	res, err := svc.IngestSample(context.Background(), domain.Sample{
		Symbol: symbol, TS: ts.UnixMicro(), Price: price, Source: src,
	})
	if err != nil {
		t.Fatalf("ingest %s @ %s: %v", src, ts, err)
	}
	return res
}

func sec(n int64) time.Time {
	return time.Unix(n, 0).UTC()
}

// TestIntegrationUnequalIntervals proves end-to-end that the reported TWAP is
// the time-weighted value, not the arithmetic mean of samples.
func TestIntegrationUnequalIntervals(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()

	// Window aligned to :00. Events at 0s (p=100) and 50s (p=200).
	base := sec(120)
	ingestAt(t, svc, clock, "AAA", base, 100, "venueA")
	ingestAt(t, svc, clock, "AAA", base.Add(50*time.Second), 200, "venueB")

	clock.Set(base.Add(60 * time.Second))
	v, err := svc.GetWindow(ctx, "AAA", base.Unix(), false)
	if err != nil {
		t.Fatal(err)
	}
	if v.TWAP != "116.666667" {
		t.Fatalf("TWAP=%s want 116.666667 (mean would wrongly be 150)", v.TWAP)
	}
	if v.Coverage != "1.000000" || v.Stale {
		t.Fatalf("coverage=%s stale=%v", v.Coverage, v.Stale)
	}
}

// TestIntegrationConstantWindow covers "整窗无变化".
func TestIntegrationConstantWindow(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()
	base := sec(300)
	ingestAt(t, svc, clock, "BBB", base, 77, "venueA")
	clock.Set(base.Add(90 * time.Second))
	v, err := svc.GetWindow(ctx, "BBB", base.Unix(), false)
	if err != nil {
		t.Fatal(err)
	}
	if v.TWAP != "77.000000" || v.Coverage != "1.000000" || v.Stale {
		t.Fatalf("constant window: %+v", v)
	}
}

// TestIntegrationWindowBoundary checks that a sample on the boundary belongs
// to the next window and the first window is carried only by its own sample.
func TestIntegrationWindowBoundary(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()
	base := sec(600)
	ingestAt(t, svc, clock, "CCC", base, 100, "venueA")
	ingestAt(t, svc, clock, "CCC", base.Add(60*time.Second), 500, "venueB")

	clock.Set(base.Add(120 * time.Second))
	v1, err := svc.GetWindow(ctx, "CCC", base.Unix(), false)
	if err != nil {
		t.Fatal(err)
	}
	v2, err := svc.GetWindow(ctx, "CCC", base.Add(60*time.Second).Unix(), false)
	if err != nil {
		t.Fatal(err)
	}
	if v1.TWAP != "100.000000" {
		t.Fatalf("first window leaked boundary price: %s", v1.TWAP)
	}
	if v2.TWAP != "500.000000" {
		t.Fatalf("second window missed boundary price: %s", v2.TWAP)
	}
}

// TestIntegrationSourceConflictPriority verifies deterministic winner
// selection and that the conflict is surfaced on read.
func TestIntegrationSourceConflictPriority(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()
	base := sec(900)
	ts := base.Add(10 * time.Second)
	ingestAt(t, svc, clock, "DDD", ts, 100, "venueC") // p0
	ingestAt(t, svc, clock, "DDD", ts, 120, "venueB") // p5
	ingestAt(t, svc, clock, "DDD", ts, 150, "venueA") // p10 -> wins

	clock.Set(base.Add(60 * time.Second))
	v, err := svc.GetWindow(ctx, "DDD", base.Unix(), false)
	if err != nil {
		t.Fatal(err)
	}
	if v.TWAP != "150.000000" {
		t.Fatalf("priority winner wrong: TWAP=%s want 150", v.TWAP)
	}
	if len(v.Conflicts) == 0 {
		t.Fatal("conflicts must be reported on the window")
	}
	// Sources list reports contributors (only winner).
	if len(v.Sources) != 1 || v.Sources[0] != "venueA" {
		t.Fatalf("sources=%v", v.Sources)
	}
}

func TestIntegrationSourceConflictRejectMode(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictReject)
	ctx := context.Background()
	base := sec(1200)
	ts := base.Add(5 * time.Second)
	ingestAt(t, svc, clock, "EEE", ts, 100, "venueA")
	clock.Set(ts.Add(time.Second))
	_, err := svc.IngestSample(ctx, domain.Sample{
		Symbol: "EEE", TS: ts.UnixMicro(), Price: 101, Source: "venueB"})
	if err == nil {
		t.Fatal("conflicting sample must be rejected in reject mode")
	}
	var ce *service.ConflictError
	if !asConflict(err, &ce) {
		t.Fatalf("want ConflictError, got %T %v", err, err)
	}
	// Identical price from another source is not a conflict.
	if _, err := svc.IngestSample(ctx, domain.Sample{
		Symbol: "EEE", TS: ts.UnixMicro(), Price: 100, Source: "venueB"}); err != nil {
		t.Fatalf("equal-price cross-source report should be accepted: %v", err)
	}
}

func asConflict(err error, target **service.ConflictError) bool {
	for err != nil {
		if c, ok := err.(*service.ConflictError); ok {
			*target = c
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

// TestIntegrationLateDataWithinFiveMinutes recomputes affected windows and
// bumps versions; beyond five minutes the sample is refused.
func TestIntegrationLateDataWithinFiveMinutes(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()
	base := sec(1800)

	ingestAt(t, svc, clock, "FFF", base, 100, "venueA")
	ingestAt(t, svc, clock, "FFF", base.Add(60*time.Second), 100, "venueA")
	ingestAt(t, svc, clock, "FFF", base.Add(120*time.Second), 100, "venueA")

	// Settle first window and capture version.
	clock.Set(base.Add(180 * time.Second))
	vBefore, err := svc.GetWindow(ctx, "FFF", base.Unix(), true)
	if err != nil {
		t.Fatal(err)
	}
	if vBefore.Version == 0 {
		t.Fatal("expected a persisted version")
	}

	// Late correction 4 minutes after "now": sample at base+10s.
	lateTS := base.Add(10 * time.Second)
	clock.Set(base.Add(180 * time.Second)) // now = base+180; late gap = 170s < 300
	res, err := svc.IngestSample(ctx, domain.Sample{
		Symbol: "FFF", TS: lateTS.UnixMicro(), Price: 200, Source: "venueA"})
	if err != nil {
		t.Fatalf("sample 170s late must be accepted: %v", err)
	}
	if len(res.NewVersions) == 0 {
		t.Fatal("late correction must create new window versions")
	}
	vAfter, err := svc.GetWindow(ctx, "FFF", base.Unix(), true)
	if err != nil {
		t.Fatal(err)
	}
	if vAfter.Version <= vBefore.Version {
		t.Fatalf("version not bumped: before=%d after=%d", vBefore.Version, vAfter.Version)
	}
	// Window integral: 100 for 10s + 200 for 50s = 11000 price*sec.
	if vAfter.TWAP != "183.333333" {
		t.Fatalf("corrected TWAP=%s want 183.333333", vAfter.TWAP)
	}
	if vAfter.ContentHash == vBefore.ContentHash {
		t.Fatal("hash must change after late correction")
	}

	// Too late: 6 minutes (>300s tolerance) -> rejected.
	tooLate := base.Add(10 * time.Second)
	clock.Set(tooLate.Add(361 * time.Second))
	_, err = svc.IngestSample(ctx, domain.Sample{
		Symbol: "GGG", TS: tooLate.UnixMicro(), Price: 1, Source: "venueA"})
	if err == nil {
		t.Fatal("sample beyond 5-minute late window must be refused")
	}
	var le *service.LateError
	if !asLate(err, &le) {
		t.Fatalf("want LateError, got %T", err)
	}
}

func asLate(err error, target **service.LateError) bool {
	if c, ok := err.(*service.LateError); ok {
		*target = c
		return true
	}
	return false
}

// TestIntegrationIncrementalEqualsFull is the key consistency requirement:
// after settling every window through the incremental per-window path, a full
// rebuild produces identical canonical content hashes.
func TestIntegrationIncrementalEqualsFull(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()

	base := sec(3600) // aligned
	schedule := []struct {
		off   time.Duration
		price int64
		src   string
	}{
		{0, 100, "venueA"},
		{20 * time.Second, 130, "venueB"},
		{60 * time.Second, 200, "venueA"}, // next window open
		{90 * time.Second, 260, "venueC"},
		// duplicate timestamp, different price, lower priority -> conflict
		{90 * time.Second, 999, "venueC"},
		{120 * time.Second, 300, "venueB"},
		{175 * time.Second, 310, "venueA"},
		{240 * time.Second, 400, "venueA"},
	}
	// Shuffle arrival order a little (insert out of order but clock moves).
	order := []int{0, 2, 1, 3, 4, 6, 5, 7}
	for _, i := range order {
		ev := schedule[i]
		ingestAt(t, svc, clock, "HHH", base.Add(ev.off), ev.price, ev.src)
	}

	// Late correction within 5 minutes to the first window.
	clock.Set(base.Add(250 * time.Second))
	if _, err := svc.IngestSample(ctx, domain.Sample{
		Symbol: "HHH", TS: base.Add(5 * time.Second).UnixMicro(),
		Price: 105, Source: "venueA",
	}); err != nil {
		t.Fatal(err)
	}
	finalNow := base.Add(300 * time.Second)
	clock.Set(finalNow)

	// Settle every window through the incremental path (read-triggered
	// recompute + version append), from the first affected window to now.
	snap := map[int64]service.WindowView{}
	for st := base.Unix(); st <= finalNow.Unix(); st += 60 {
		v, err := svc.GetWindow(ctx, "HHH", st, true)
		if err != nil {
			t.Fatal(err)
		}
		if v.CoveredUsec > 0 {
			snap[st] = *v
		}
	}

	if _, err := svc.FullRebuild(ctx); err != nil {
		t.Fatal(err)
	}

	for st := base.Unix(); st <= finalNow.Unix(); st += 60 {
		v, err := svc.GetWindow(ctx, "HHH", st, false)
		if err != nil {
			t.Fatal(err)
		}
		if v.CoveredUsec == 0 {
			if _, had := snap[st]; had {
				t.Fatalf("window %d materialized incrementally but empty after rebuild", st)
			}
			continue
		}
		inc, ok := snap[st]
		if !ok {
			t.Fatalf("window %d present after rebuild but not incrementally", st)
		}
		if v.ContentHash != inc.ContentHash {
			t.Fatalf("window %d hash mismatch incremental=%s full=%s\ninc: %+v\nfull: %+v",
				st, inc.ContentHash, v.ContentHash, inc, v)
		}
		if v.TWAP != inc.TWAP || v.Coverage != inc.Coverage || v.Stale != inc.Stale {
			t.Fatalf("window %d field mismatch: inc(twap=%s cov=%s stale=%v) full(twap=%s cov=%s stale=%v)",
				st, inc.TWAP, inc.Coverage, inc.Stale, v.TWAP, v.Coverage, v.Stale)
		}
	}

	// Spot-check one computed value: window [60,120):
	// carry 105? No — window 60 starts with sample at 60 p=200, event at 90:
	// venueB? schedule[3] venueC 260 p0, schedule[4] same ts venueC 999
	// (same source, updates to 999). So p=200 for 30s, p=999 for 30s.
	v60, err := svc.GetWindow(ctx, "HHH", base.Add(60*time.Second).Unix(), false)
	if err != nil {
		t.Fatal(err)
	}
	if v60.TWAP != "599.500000" {
		t.Fatalf("spot window [60,120) TWAP=%s want 599.500000", v60.TWAP)
	}
}

// TestIntegrationCoverageAndStale verifies read output for an empty window
// (no carry-in) and a window cut short by the staleness horizon.
func TestIntegrationCoverageAndStale(t *testing.T) {
	svc, _, clock := setupIntegration(t, service.ConflictPriority)
	ctx := context.Background()
	base := sec(5400)

	// No data at all: empty, coverage 0, stale, no version.
	v, err := svc.GetWindow(ctx, "III", base.Unix(), true)
	if err != nil {
		t.Fatal(err)
	}
	if v.CoveredUsec != 0 || !v.Stale || v.Version != 0 || v.TWAP != "" {
		t.Fatalf("empty window wrong: %+v", v)
	}

	// One sample at window start, clock 60s ahead: horizon 120s still covers
	// the whole window -> not stale.
	ingestAt(t, svc, clock, "III", base, 50, "venueA")
	clock.Set(base.Add(60 * time.Second))
	v, _ = svc.GetWindow(ctx, "III", base.Unix(), false)
	if v.Coverage != "1.000000" || v.Stale {
		t.Fatalf("fresh carry window: %+v", v)
	}

	// Advance another 90s with no new data; reading the NEXT window
	// [+60,+120): carry is 150s old by its end -> coverage cut at 120s age,
	// i.e. it covers the first 60s of that window fully.
	clock.Set(base.Add(150 * time.Second))
	v, _ = svc.GetWindow(ctx, "III", base.Add(60*time.Second).Unix(), false)
	if v.CoveredUsec != 60*domain.MicroPerSec {
		t.Fatalf("expected 60s covered, got %d", v.CoveredUsec/domain.MicroPerSec)
	}
	if !v.Stale || v.TWAP != "50.000000" {
		t.Fatalf("stale carry window: %+v", v)
	}
}
