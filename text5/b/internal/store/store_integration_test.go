package store_test

// Integration tests run against a real PostgreSQL database. They are skipped
// unless TEST_DATABASE_URL is set, e.g.:
//
//	docker run -d --name desklens-test-pg -p 55432:5432 \
//	  -e POSTGRES_USER=desklens -e POSTGRES_PASSWORD=desklens -e POSTGRES_DB=desklens postgres:16-alpine
//	TEST_DATABASE_URL='postgres://desklens:desklens@localhost:55432/desklens?sslmode=disable' go test ./...
import (
	"context"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"

	"desklens/internal/classify"
	"desklens/internal/model"
	"desklens/internal/store"
	"desklens/migrations"
)

func testStore(t *testing.T) *store.Store {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set")
	}
	db, err := sqlx.Connect("postgres", dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { db.Close() })

	// Fresh schema per test.
	if _, err := db.Exec(`
		DROP TABLE IF EXISTS raw_snapshots, employee_daily_summary, department_weekly_summary,
			classification_rules, classification_versions, policy_versions,
			retention_state, users, employees, departments, schema_migrations CASCADE`); err != nil {
		t.Fatalf("reset schema: %v", err)
	}
	if err := migrations.Apply(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// Fixtures: dept 1 (Engineering, monitored), dept 2 (Executive, exempt).
	if _, err := db.Exec(`
		INSERT INTO departments (id, name) VALUES (1, 'Engineering'), (2, 'Executive');
		INSERT INTO employees (id, department_id, name, timezone) VALUES
			(1, 1, 'Alice', 'Asia/Shanghai'),
			(2, 1, 'Bob',   'UTC'),
			(3, 2, 'Dave',  'UTC');
		INSERT INTO policy_versions (work_start_minutes, work_end_minutes, workdays, excluded_apps, exempt_department_ids)
		VALUES (9*60, 18*60, ARRAY[1,2,3,4,5]::smallint[], ARRAY['1Password'], ARRAY[2]::int[]);`); err != nil {
		t.Fatalf("fixtures: %v", err)
	}
	st := store.New(db)
	if _, err := st.PublishClassification(context.Background(), []classify.Rule{
		{Pattern: "*Code*", Category: classify.Productive, Priority: 10},
		{Pattern: "*YouTube*", Category: classify.Unproductive, Priority: 10},
	}); err != nil {
		t.Fatalf("classification: %v", err)
	}
	return st
}

func snap(ws string, emp int64, ts string, app string, n int64) model.SnapshotInput {
	t, err := time.Parse(time.RFC3339, ts)
	if err != nil {
		panic(err)
	}
	return model.SnapshotInput{WorkstationID: ws, EmployeeID: emp, CapturedAt: t, AppName: app, ActivityCount: n}
}

func dailyTotal(t *testing.T, st *store.Store, empID int64, day string) (prod, unprod, total int64) {
	t.Helper()
	d, _ := time.Parse("2006-01-02", day)
	rows, err := st.DailySummaries(context.Background(), empID, d, d)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) == 0 {
		return 0, 0, 0
	}
	return rows[0].Productive, rows[0].Unproductive, rows[0].Total
}

// Privacy: excluded apps, exempt departments and out-of-window snapshots
// must never reach the raw table.
func TestPrivacyFiltering(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()

	res, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T01:00:00Z", "VS Code", 10),  // Mon 09:00 Shanghai: kept
		snap("ws1", 1, "2026-09-14T02:00:00Z", "1Password", 5), // excluded app
		snap("ws1", 1, "2026-09-14T10:00:00Z", "VS Code", 5),   // 18:00 Shanghai: outside window
		snap("ws1", 1, "2026-09-13T03:00:00Z", "VS Code", 5),   // Sunday local
		snap("ws3", 3, "2026-09-14T10:00:00Z", "VS Code", 5),   // exempt department
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Inserted != 1 || res.Filtered != 4 {
		t.Fatalf("got inserted=%d filtered=%d, want 1/4", res.Inserted, res.Filtered)
	}
	var rawCount int
	if err := st.DB().Get(&rawCount, `SELECT COUNT(*) FROM raw_snapshots`); err != nil {
		t.Fatal(err)
	}
	if rawCount != 1 {
		t.Fatalf("raw table holds %d rows, want 1 (filtered data must never be persisted)", rawCount)
	}
}

// A snapshot near midnight UTC lands on the employee's local day.
func TestCrossDayTimezone(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()

	// 2026-09-14 16:30 UTC = 2026-09-15 00:30 in Shanghai -> outside window, filtered.
	// 2026-09-14 15:59 UTC = 23:59 Shanghai: outside window too. Use 09:00-18:00 window
	// boundaries instead: 09:00 Shanghai = 01:00 UTC; 17:30 Shanghai = 09:30 UTC.
	// To exercise the local-date boundary we temporarily publish a 24h-ish window.
	if _, err := st.PublishPolicy(ctx, 0, 24*60-1, []int64{0, 1, 2, 3, 4, 5, 6}, nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T16:30:00Z", "VS Code", 7), // 2026-09-15 00:30 Shanghai
		snap("ws2", 2, "2026-09-14T16:30:00Z", "VS Code", 3), // 2026-09-14 16:30 UTC (Bob is UTC)
	}); err != nil {
		t.Fatal(err)
	}

	if _, _, total := dailyTotal(t, st, 1, "2026-09-15"); total != 7 {
		t.Errorf("Alice's 2026-09-15 total = %d, want 7", total)
	}
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != 0 {
		t.Errorf("Alice's 2026-09-14 total = %d, want 0", total)
	}
	if _, _, total := dailyTotal(t, st, 2, "2026-09-14"); total != 3 {
		t.Errorf("Bob's 2026-09-14 total = %d, want 3", total)
	}
}

// Idempotent re-delivery is a no-op; conflicting content rejects the batch
// and rolls everything back.
func TestIdempotencyAndConflict(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()
	batch := []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T01:00:00Z", "VS Code", 10),
		snap("ws1", 1, "2026-09-14T01:01:00Z", "VS Code", 20),
	}
	res, err := st.IngestBatch(ctx, batch)
	if err != nil || res.Inserted != 2 {
		t.Fatalf("first ingest: %+v err=%v", res, err)
	}
	res, err = st.IngestBatch(ctx, batch)
	if err != nil || res.Duplicates != 2 || res.Inserted != 0 {
		t.Fatalf("re-delivery should be duplicates: %+v err=%v", res, err)
	}
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != 30 {
		t.Fatalf("total after duplicate delivery = %d, want 30 (processed once)", total)
	}

	// Conflict: same key, different content -> 409-style error, whole batch
	// (including the valid new row) rolled back.
	conflicting := []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T01:00:00Z", "VS Code", 999), // same key, different count
		snap("ws1", 1, "2026-09-14T01:02:00Z", "VS Code", 5),   // valid new row
	}
	_, err = st.IngestBatch(ctx, conflicting)
	var conflictErr store.ErrConflict
	if !errors.As(err, &conflictErr) {
		t.Fatalf("want ErrConflict, got %v", err)
	}
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != 30 {
		t.Fatalf("conflicting batch must roll back entirely: total = %d, want 30", total)
	}
}

// Late snapshots recompute only their own dates; earlier summaries stay put.
func TestLateArrivalRecomputesAffectedDayOnly(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()

	if _, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-15T02:00:00Z", "VS Code", 10), // Tue 10:00 Shanghai
	}); err != nil {
		t.Fatal(err)
	}
	// Late snapshot for the previous day arrives afterwards.
	if _, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T02:00:00Z", "YouTube", 4), // Mon 10:00 Shanghai
	}); err != nil {
		t.Fatal(err)
	}

	prod, unprod, total := dailyTotal(t, st, 1, "2026-09-14")
	if unprod != 4 || total != 4 || prod != 0 {
		t.Errorf("late day: prod=%d unprod=%d total=%d, want 0/4/4", prod, unprod, total)
	}
	if _, _, total := dailyTotal(t, st, 1, "2026-09-15"); total != 10 {
		t.Errorf("unaffected day changed: total = %d, want 10", total)
	}
}

// New classification versions apply only to newly ingested rows; historical
// rows keep the version and category they were classified with.
func TestClassificationVersioning(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()

	if _, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T01:00:00Z", "Fancy Editor", 10), // neutral under v1
	}); err != nil {
		t.Fatal(err)
	}

	// v2 reclassifies "Fancy Editor" as productive.
	if _, err := st.PublishClassification(ctx, []classify.Rule{
		{Pattern: "*Code*", Category: classify.Productive, Priority: 10},
		{Pattern: "*YouTube*", Category: classify.Unproductive, Priority: 10},
		{Pattern: "Fancy*", Category: classify.Productive, Priority: 20},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T01:01:00Z", "Fancy Editor", 5), // productive under v2
	}); err != nil {
		t.Fatal(err)
	}

	var rows []struct {
		Minute  time.Time `db:"minute_utc"`
		Cat     string    `db:"category"`
		Version int       `db:"classification_version"`
	}
	if err := st.DB().Select(&rows, `
		SELECT minute_utc, category, classification_version
		FROM raw_snapshots ORDER BY minute_utc`); err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Fatalf("want 2 raw rows, got %d", len(rows))
	}
	if rows[0].Cat != "neutral" || rows[0].Version != 1 {
		t.Errorf("history must not be rewritten: row0 = %+v", rows[0])
	}
	if rows[1].Cat != "productive" || rows[1].Version != 2 {
		t.Errorf("new row should use v2: row1 = %+v", rows[1])
	}
	// Daily summary reflects the mixed categories of the raw rows.
	prod, _, total := dailyTotal(t, st, 1, "2026-09-14")
	if prod != 5 || total != 15 {
		t.Errorf("daily summary prod=%d total=%d, want 5/15", prod, total)
	}
}

// Rebuild from raw must match incremental results, and a rebuild running
// concurrently with new ingestion must not lose or double-count writes.
func TestRebuildConsistencyAndConcurrency(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()

	var batch []model.SnapshotInput
	for i := 0; i < 20; i++ {
		batch = append(batch, snap("ws1", 1,
			time.Date(2026, 9, 14, 1, i, 0, 0, time.UTC).Format(time.RFC3339), "VS Code", int64(i+1)))
	}
	if _, err := st.IngestBatch(ctx, batch); err != nil {
		t.Fatal(err)
	}
	want := int64(20 * 21 / 2)
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != want {
		t.Fatalf("incremental total = %d, want %d", total, want)
	}

	// Rebuild while a new snapshot for the same day is being ingested.
	day, _ := time.Parse("2006-01-02", "2026-09-14")
	var wg sync.WaitGroup
	wg.Add(2)
	var rebuildErr, ingestErr error
	go func() {
		defer wg.Done()
		for i := 0; i < 5; i++ {
			if _, rebuildErr = st.Rebuild(ctx, day, day); rebuildErr != nil {
				return
			}
		}
	}()
	go func() {
		defer wg.Done()
		_, ingestErr = st.IngestBatch(ctx, []model.SnapshotInput{
			snap("ws1", 1, "2026-09-14T03:00:00Z", "VS Code", 100),
		})
	}()
	wg.Wait()
	if rebuildErr != nil || ingestErr != nil {
		t.Fatalf("rebuildErr=%v ingestErr=%v", rebuildErr, ingestErr)
	}

	// Final state must equal a clean rebuild of all raw data.
	if _, err := st.Rebuild(ctx, day, day); err != nil {
		t.Fatal(err)
	}
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != want+100 {
		t.Fatalf("after concurrent rebuild+ingest: total = %d, want %d (no lost/duplicate writes)", total, want+100)
	}

	// Weekly summary agrees with the daily ones.
	weeks, err := st.WeeklySummaries(ctx, 1, day, day.AddDate(0, 0, 7))
	if err != nil {
		t.Fatal(err)
	}
	if len(weeks) != 1 || weeks[0].Total != want+100 {
		t.Fatalf("weekly summary = %+v, want one week totalling %d", weeks, want+100)
	}
}

// Cleanup removes raw rows but keeps summaries, advances the rebuildable
// cutoff, and refuses rebuilds that reach into cleaned-up history.
func TestCleanupBoundary(t *testing.T) {
	st := testStore(t)
	ctx := context.Background()

	if _, err := st.IngestBatch(ctx, []model.SnapshotInput{
		snap("ws1", 1, "2026-09-14T01:00:00Z", "VS Code", 10), // Mon
		snap("ws1", 1, "2026-09-15T01:00:00Z", "VS Code", 20), // Tue
	}); err != nil {
		t.Fatal(err)
	}

	cutoff := time.Date(2026, 9, 15, 0, 0, 0, 0, time.UTC)
	deleted, err := st.Cleanup(ctx, cutoff)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 1 {
		t.Fatalf("deleted %d raw rows, want 1", deleted)
	}

	// Summaries survive cleanup.
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != 10 {
		t.Fatalf("summary for cleaned day lost: total = %d, want 10", total)
	}

	// Rebuild reaching before the cutoff is refused...
	day14, _ := time.Parse("2006-01-02", "2026-09-14")
	day15, _ := time.Parse("2006-01-02", "2026-09-15")
	var beforeCutoff store.ErrBeforeCutoff
	if _, err := st.Rebuild(ctx, day14, day15); !errors.As(err, &beforeCutoff) {
		t.Fatalf("rebuild before cutoff should fail with ErrBeforeCutoff, got %v", err)
	}
	// ...but the still-rebuildable range works and reproduces the summary.
	if _, err := st.Rebuild(ctx, day15, day15); err != nil {
		t.Fatal(err)
	}
	if _, _, total := dailyTotal(t, st, 1, "2026-09-15"); total != 20 {
		t.Fatalf("rebuilt day total = %d, want 20", total)
	}
	// And the refused rebuild did not clobber the older summary with
	// partial (empty) history.
	if _, _, total := dailyTotal(t, st, 1, "2026-09-14"); total != 10 {
		t.Fatalf("cleaned day's summary was overwritten: total = %d, want 10", total)
	}
}
