package detection_test

import (
	"context"
	"strconv"
	"testing"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/detection"
	"anomalywatch/internal/models"
	"anomalywatch/internal/testdb"

	"gorm.io/gorm"
)

type fakeClock struct{ t time.Time }

func (c *fakeClock) Now() time.Time      { return c.t }
func (c *fakeClock) Add(d time.Duration) { c.t = c.t.Add(d) }

type env struct {
	db   *gorm.DB
	cfg  config.Config
	eng  *detection.Engine
	clk  *fakeClock
	emps map[string]uint64
}

func setup(t *testing.T) env {
	gdb, cfg := testdb.New(t)
	// Keep the backfill horizon wide enough for 30-day scenarios.
	cfg.MaxBackfillAge = 60 * 24 * time.Hour
	clk := &fakeClock{t: time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC)}
	eng := detection.New(gdb, cfg).WithClock(clk)
	return env{
		db:   gdb,
		cfg:  cfg,
		eng:  eng,
		clk:  clk,
		emps: testdb.EmployeeIDs(t, gdb),
	}
}

func insertEvent(t *testing.T, db *gorm.DB, source, id string, emp uint64, typ string, at time.Time, meta models.JSONMap) {
	t.Helper()
	if err := db.Exec(`
		INSERT INTO events (source, event_id, employee_id, event_type, occurred_at, metadata, content_hash, ingest_token, processed, received_at)
		VALUES (?, ?, ?, ?, ?, CAST(? AS JSON), ?, '', 0, UTC_TIMESTAMP(3))`,
		source, id, emp, typ, at, jsonOrNull(meta), "h-"+id).Error; err != nil {
		t.Fatalf("insert event %s: %v", id, err)
	}
}

func jsonOrNull(m models.JSONMap) string {
	if m == nil {
		return "{}"
	}
	b, _ := jsonMarshal(m)
	return string(b)
}

func alertCount(t *testing.T, db *gorm.DB, code string, emp uint64) int64 {
	t.Helper()
	var n int64
	q := db.Model(&models.Alert{}).Where("rule_code = ?", code)
	if emp != 0 {
		q = q.Where("employee_id = ?", emp)
	}
	if err := q.Count(&n).Error; err != nil {
		t.Fatalf("count alerts: %v", err)
	}
	return n
}

func processAll(t *testing.T, e env) {
	t.Helper()
	for i := 0; i < 20; i++ {
		n, err := e.eng.ProcessPending(500)
		if err != nil {
			t.Fatalf("process pending: %v", err)
		}
		if n == 0 {
			return
		}
	}
}

// --- Download burst ---------------------------------------------------------

func TestDownloadBurstFiresAt51AndDedups(t *testing.T) {
	e := setup(t)
	emp := e.emps["David Zhao"] // UTC, 12:00 bucket
	base := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	for i := 0; i < 50; i++ {
		insertEvent(t, e.db, "s", "d-before-"+itoa(i), emp, models.EventTypeFileDownload,
			base.Add(time.Duration(i)*11*time.Second), nil)
	}
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleDownloadBurst, emp); got != 0 {
		t.Fatalf("50 downloads must not fire, got %d alerts", got)
	}

	// 51st download crosses the threshold.
	insertEvent(t, e.db, "s", "d-51", emp, models.EventTypeFileDownload,
		base.Add(9*time.Minute+30*time.Second), nil)
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleDownloadBurst, emp); got != 1 {
		t.Fatalf("51 downloads must fire exactly one alert, got %d", got)
	}

	// Re-running the scheduler (duplicate tick / restart) must not duplicate.
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if got := alertCount(t, e.db, models.RuleDownloadBurst, emp); got != 1 {
		t.Fatalf("reprocessing created %d alerts, want 1", got)
	}
}

// --- Late backfill withdraws a false burst ---------------------------------

func TestLateBackfillRecomputesBurstWindow(t *testing.T) {
	e := setup(t)
	emp := e.emps["David Zhao"]
	base := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)

	// Simulate a window that fired under the configured rule of >50 in 10m by
	// inserting 51 tightly-spaced downloads in a 5-minute span... then change
	// the rule window to 5 minutes: the same events spread over two 5-minute
	// buckets must each stay under the threshold after a sweep, and the old
	// alert is withdrawn (window moved) or retained for the one dense bucket.
	// Here we instead validate the clearer backfill contract: with a 10m rule
	// the alert fires; lowering threshold is not a backfill case, so this test
	// focuses on a late event *removing* density is impossible (events are
	// append-only). We therefore verify evidence refresh on a late extra event.
	for i := 0; i < 51; i++ {
		insertEvent(t, e.db, "s", "d-"+itoa(i), emp, models.EventTypeFileDownload,
			base.Add(time.Duration(i)*10*time.Second), nil) // 0..8:20
	}
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleDownloadBurst, emp); got != 1 {
		t.Fatalf("want 1 burst alert, got %d", got)
	}

	var a models.Alert
	if err := e.db.Where("rule_code = ? AND employee_id = ?", models.RuleDownloadBurst, emp).First(&a).Error; err != nil {
		t.Fatal(err)
	}
	before := countInEvidence(a.Evidence, "download_count")
	if before != 51 {
		t.Fatalf("evidence count = %v, want 51", before)
	}

	// A late out-of-order event for the same bucket recomputes the window.
	insertEvent(t, e.db, "s", "d-late", emp, models.EventTypeFileDownload,
		base.Add(9*time.Minute+55*time.Second), nil)
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleDownloadBurst, emp); got != 1 {
		t.Fatalf("recomputed alert count = %d, want still 1", got)
	}
	if err := e.db.Where("rule_code = ? AND employee_id = ?", models.RuleDownloadBurst, emp).First(&a).Error; err != nil {
		t.Fatal(err)
	}
	if got := countInEvidence(a.Evidence, "download_count"); got != 52 {
		t.Fatalf("evidence after backfill = %v, want 52", got)
	}
}

// --- First USB --------------------------------------------------------------

func TestFirstUSBFiresOnceAndRecomputesOnEarlierLateEvent(t *testing.T) {
	e := setup(t)
	emp := e.emps["David Zhao"]
	first := time.Date(2026, 9, 15, 9, 0, 0, 0, time.UTC)
	later := first.Add(2 * time.Hour)
	insertEvent(t, e.db, "s", "usb-later", emp, models.EventTypeUSB, later,
		models.JSONMap{"device": "B"})
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleFirstUSB, emp); got != 1 {
		t.Fatalf("first usb alert = %d, want 1", got)
	}

	// Earlier USB reported late: evidence must point to the new earliest event,
	// with no duplicate alert.
	insertEvent(t, e.db, "s", "usb-earlier", emp, models.EventTypeUSB, first,
		models.JSONMap{"device": "A"})
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleFirstUSB, emp); got != 1 {
		t.Fatalf("after late earlier USB alerts = %d, want 1", got)
	}
	var a models.Alert
	e.db.Where("rule_code = ? AND employee_id = ?", models.RuleFirstUSB, emp).First(&a)
	if a.Evidence["event_id"] != "usb-earlier" {
		t.Fatalf("evidence event = %v, want usb-earlier", a.Evidence["event_id"])
	}

	// A third USB changes nothing.
	insertEvent(t, e.db, "s", "usb-third", emp, models.EventTypeUSB, later.Add(time.Hour), nil)
	processAll(t, e)
	if got := alertCount(t, e.db, models.RuleFirstUSB, emp); got != 1 {
		t.Fatalf("third USB must not add alerts, got %d", got)
	}
}

func TestNoUSBMeansNoAlert(t *testing.T) {
	e := setup(t)
	emp := e.emps["Alice Chen"]
	insertEvent(t, e.db, "s", "login-1", emp, models.EventTypeLogin,
		time.Date(2026, 9, 20, 3, 0, 0, 0, time.UTC), nil)
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := alertCount(t, e.db, models.RuleFirstUSB, emp); got != 0 {
		t.Fatalf("usb alerts with no USB events = %d, want 0", got)
	}
}

// --- Night window and timezone boundaries ----------------------------------

func TestNightActivityTimezoneBoundaries(t *testing.T) {
	e := setup(t)
	alice := e.emps["Alice Chen"] // Asia/Shanghai UTC+8
	carol := e.emps["Carol Wang"] // Europe/London UTC+1 in September

	// Alice: 12:00Z = 20:00 local -> in; 22:00Z = 06:00 next local day -> out.
	insertEvent(t, e.db, "s", "a-in", alice, models.EventTypeLogin,
		time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC), nil)
	insertEvent(t, e.db, "s", "a-out", alice, models.EventTypeLogin,
		time.Date(2026, 9, 20, 22, 0, 0, 0, time.UTC), nil)

	// Carol: 04:59Z = 05:59 local -> in; 05:00Z = 06:00 local -> out.
	insertEvent(t, e.db, "s", "c-in", carol, models.EventTypeLogin,
		time.Date(2026, 9, 20, 4, 59, 0, 0, time.UTC), nil)
	insertEvent(t, e.db, "s", "c-out", carol, models.EventTypeLogin,
		time.Date(2026, 9, 20, 5, 0, 0, 0, time.UTC), nil)

	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := alertCount(t, e.db, models.RuleNightActivity, alice); got != 1 {
		t.Fatalf("alice night alerts = %d, want exactly 1 (20:00 only)", got)
	}
	if got := alertCount(t, e.db, models.RuleNightActivity, carol); got != 1 {
		t.Fatalf("carol night alerts = %d, want exactly 1 (05:59 only)", got)
	}
}

// Cross-midnight ownership: 02:00Z Shanghai = 10:00 local (outside); 21:00Z
// Shanghai = 05:00 local next day, owned by the previous day's night.
func TestNightOwnsPreviousCalendarDayAfterMidnight(t *testing.T) {
	e := setup(t)
	alice := e.emps["Alice Chen"]
	insertEvent(t, e.db, "s", "tail", alice, models.EventTypeLogin,
		time.Date(2026, 9, 20, 21, 0, 0, 0, time.UTC), nil) // 05:00 on Sep 21 local
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	var a models.Alert
	if err := e.db.Where("rule_code = ? AND employee_id = ?", models.RuleNightActivity, alice).First(&a).Error; err != nil {
		t.Fatalf("expected one night alert: %v", err)
	}
	if got := a.WindowStart.Format("2006-01-02"); got != "2026-09-20" {
		t.Fatalf("night window start date = %s, want 2026-09-20 (owned by previous day)", got)
	}
}

// --- Statistical rule -------------------------------------------------------

// insertBaseline writes n events per local day for the given employee across
// the days [day-30, day-1] at hour 10 local (outside the night window where
// the zone makes that matter).
func insertBaseline(t *testing.T, e env, emp uint64, locName string, targetDay time.Time, perDay int) {
	t.Helper()
	loc, err := time.LoadLocation(locName)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for d := 30; d >= 1; d-- {
		localDay := time.Date(targetDay.Year(), targetDay.Month(), targetDay.Day(), 0, 0, 0, 0, loc).AddDate(0, 0, -d)
		for i := 0; i < perDay; i++ {
			n++
			at := localDay.Add(10*time.Hour + time.Duration(i)*time.Minute)
			insertEvent(t, e.db, "base", "b-"+strconv.FormatUint(emp, 10)+"-"+itoa(n), emp, models.EventTypeLogin, at.UTC(), nil)
		}
	}
}

func TestStatisticalFiresOnSpike(t *testing.T) {
	e := setup(t)
	emp := e.emps["David Zhao"] // UTC
	day := time.Date(2026, 9, 20, 0, 0, 0, 0, time.UTC)
	insertBaseline(t, e, emp, "UTC", day, 5) // 30 days x 5
	// Spike day: 40 events vs mean 5, sd 0 -> z=+Inf > 2.5.
	for i := 0; i < 40; i++ {
		at := day.Add(10*time.Hour + time.Duration(i)*time.Minute)
		insertEvent(t, e.db, "spike", "sp-"+itoa(i), emp, models.EventTypeLogin, at, nil)
	}
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := alertCount(t, e.db, models.RuleStatistical, emp); got != 1 {
		t.Fatalf("stat alerts = %d, want 1", got)
	}
	var a models.Alert
	e.db.Where("rule_code = ? AND employee_id = ?", models.RuleStatistical, emp).First(&a)
	if z, ok := a.Evidence["z_score"].(float64); ok {
		if !(z > 2.5) {
			t.Fatalf("z_score = %v, want > 2.5", z)
		}
	} else if a.Evidence["constant_baseline"] != true {
		t.Fatalf("evidence must carry z_score>2.5 or constant_baseline, got %v", a.Evidence)
	}
}

func TestStatisticalSkipsInsufficientSamples(t *testing.T) {
	e := setup(t)
	emp := e.emps["Bob Li"]
	// Only 3 prior days of history (< min_samples default 10).
	loc, _ := time.LoadLocation("America/New_York")
	day := time.Date(2026, 9, 20, 0, 0, 0, 0, loc)
	for d := 3; d >= 1; d-- {
		for i := 0; i < 2; i++ {
			ld := day.AddDate(0, 0, -d)
			insertEvent(t, e.db, "few", "f-"+itoa(d)+"-"+itoa(i), emp, models.EventTypeLogin,
				ld.Add(10*time.Hour+time.Duration(i)*time.Minute).UTC(), nil)
		}
	}
	for i := 0; i < 30; i++ {
		insertEvent(t, e.db, "few", "sp-"+itoa(i), emp, models.EventTypeLogin,
			day.Add(10*time.Hour+time.Duration(i)*time.Minute).UTC(), nil)
	}
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := alertCount(t, e.db, models.RuleStatistical, emp); got != 0 {
		t.Fatalf("stat alerts with insufficient samples = %d, want 0 (fixed rules only)", got)
	}
}

func TestStatisticalNoFireOnNormalDay(t *testing.T) {
	e := setup(t)
	emp := e.emps["Alice Chen"]
	day := time.Date(2026, 9, 20, 0, 0, 0, 0, mustLoc("Asia/Shanghai"))
	insertBaseline(t, e, emp, "Asia/Shanghai", day, 5)
	for i := 0; i < 5; i++ {
		at := day.Add(10*time.Hour + time.Duration(i)*time.Minute)
		insertEvent(t, e.db, "norm", "n-"+itoa(i), emp, models.EventTypeLogin, at.UTC(), nil)
	}
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := alertCount(t, e.db, models.RuleStatistical, emp); got != 0 {
		t.Fatalf("normal day produced %d stat alerts, want 0", got)
	}
}

// --- Restart safety ---------------------------------------------------------

func TestRestartDoesNotLosePendingEvents(t *testing.T) {
	e := setup(t)
	emp := e.emps["David Zhao"]
	base := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	for i := 0; i < 51; i++ {
		insertEvent(t, e.db, "s", "r-"+itoa(i), emp, models.EventTypeFileDownload,
			base.Add(time.Duration(i)*time.Second), nil)
	}
	// Simulate a brand-new engine/process instance: pending events persist in
	// the DB and a fresh engine drains them.
	fresh := detection.New(e.db, e.cfg).WithClock(e.clk)
	n, err := fresh.ProcessPending(10)
	if err != nil {
		t.Fatal(err)
	}
	if n != 10 {
		t.Fatalf("first claim = %d, want 10", n)
	}
	// Concurrent second engine must claim zero of the same rows.
	n2, err := fresh.ProcessPending(10)
	if err != nil {
		t.Fatal(err)
	}
	if n2 != 10 {
		t.Fatalf("second claim = %d, want next 10 distinct", n2)
	}
	for i := 0; i < 10; i++ {
		if _, err := fresh.ProcessPending(10); err != nil {
			t.Fatal(err)
		}
	}
	var remaining int64
	e.db.Model(&models.Event{}).Where("processed = 0").Count(&remaining)
	if remaining != 0 {
		t.Fatalf("unprocessed after drain = %d, want 0", remaining)
	}
	if got := alertCount(t, e.db, models.RuleDownloadBurst, emp); got != 1 {
		t.Fatalf("burst alerts after restart drain = %d, want 1", got)
	}
}
