// Package tests contains Postgres-backed integration tests. They run against
// the database named by TEST_DATABASE_URL (see docker-compose `test`
// service); without it they are skipped so `go test ./...` still works.
package tests

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"desklens/internal/aggregate"
	"desklens/internal/api"
	"desklens/internal/db"
	"desklens/internal/ingest"
	"desklens/migrations"
)

// Fixture ids (mirrors seed/seed.sql, kept minimal for tests).
const (
	deptEng   = 1
	deptSales = 2 // exempt

	empAdmin = 1
	empMgr   = 2
	empAlice = 3 // Asia/Shanghai
	empBob   = 4 // UTC
	empEve   = 5 // Sales (exempt)
)

func testDB(t *testing.T) *sqlx.DB {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping integration test")
	}
	ctx := context.Background()
	database, err := db.Connect(ctx, url)
	require.NoError(t, err)
	t.Cleanup(func() { database.Close() })

	// Fresh schema per test for full isolation.
	_, err = database.ExecContext(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`)
	require.NoError(t, err)
	require.NoError(t, db.Migrate(ctx, database, migrations.FS))

	_, err = database.ExecContext(ctx, `
		INSERT INTO departments (id, name, exempt) VALUES
			(1, 'Engineering', FALSE), (2, 'Sales', TRUE);
		INSERT INTO employees (id, department_id, name, timezone, role) VALUES
			(1, 1, 'Admin', 'UTC', 'admin'),
			(2, 1, 'Mgr',   'UTC', 'manager'),
			(3, 1, 'Alice', 'Asia/Shanghai', 'employee'),
			(4, 1, 'Bob',   'UTC', 'employee'),
			(5, 2, 'Eve',   'UTC', 'employee');
		INSERT INTO workstations (id, employee_id) VALUES
			('ws-alice', 3), ('ws-bob', 4), ('ws-eve', 5);
		INSERT INTO api_tokens (token, employee_id) VALUES
			('token-admin', 1), ('token-mgr', 2), ('token-alice', 3), ('token-bob', 4);`)
	require.NoError(t, err)
	return database
}

func snap(ws string, emp int64, ts time.Time, app string, count int) ingest.Snapshot {
	return ingest.Snapshot{WorkstationID: ws, EmployeeID: emp, Timestamp: ts, AppName: app, ActivityCount: count}
}

func utc(y int, m time.Month, d, h, min int) time.Time {
	return time.Date(y, m, d, h, min, 0, 0, time.UTC)
}

func rawCount(t *testing.T, database *sqlx.DB) int {
	t.Helper()
	var n int
	require.NoError(t, database.Get(&n, `SELECT COUNT(*) FROM raw_snapshots`))
	return n
}

type dailySummary struct {
	Prod, Unprod, Neutral, Total int64
	Snapshots                    int
}

func getDaily(t *testing.T, database *sqlx.DB, emp int64, day string) (dailySummary, bool) {
	t.Helper()
	var row struct {
		Prod     int64 `db:"productive_count"`
		Unprod   int64 `db:"unproductive_count"`
		Neutral  int64 `db:"neutral_count"`
		Total    int64 `db:"total_count"`
		Snapshot int   `db:"snapshots"`
	}
	err := database.Get(&row, `
		SELECT productive_count, unproductive_count, neutral_count, total_count, snapshots
		FROM daily_summaries WHERE employee_id = $1 AND day = $2`, emp, day)
	if err != nil {
		return dailySummary{}, false
	}
	return dailySummary{row.Prod, row.Unprod, row.Neutral, row.Total, row.Snapshot}, true
}

// --- privacy filtering ------------------------------------------------------

func TestPrivacyFiltering(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	// Default policy v1: window 09:00-18:00 local, no excluded apps.
	// Alice is UTC+8, so 01:30 UTC = 09:30 local (inside), 11:00 UTC = 19:00 local (outside).
	res, err := ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-alice", empAlice, utc(2026, 1, 5, 1, 30), "VS Code", 10),   // accepted
		snap("ws-alice", empAlice, utc(2026, 1, 5, 11, 0), "VS Code", 5),    // outside window
		snap("ws-eve", empEve, utc(2026, 1, 5, 10, 0), "VS Code", 7),        // exempt department
	})
	require.NoError(t, err)
	assert.Equal(t, 1, res.Accepted)
	assert.Equal(t, 1, res.PolicyVersion)
	require.Len(t, res.Filtered, 2)
	reasons := map[string]bool{}
	for _, f := range res.Filtered {
		reasons[f.Reason] = true
	}
	assert.True(t, reasons["outside_monitoring_window"])
	assert.True(t, reasons["department_exempt"])
	assert.Equal(t, 1, rawCount(t, database), "filtered rows must never reach the raw table")

	// Publish policy v2 excluding games; v2 must apply only to NEW ingestion.
	_, err = database.Exec(`
		INSERT INTO policies (version, work_start, work_end, excluded_apps)
		VALUES (2, '09:00', '18:00', '["*game*"]')`)
	require.NoError(t, err)

	res, err = ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 6, 10, 0), "SuperGame", 3),  // excluded by v2
		snap("ws-bob", empBob, utc(2026, 1, 6, 10, 1), "Terminal", 4),   // accepted under v2
	})
	require.NoError(t, err)
	assert.Equal(t, 1, res.Accepted)
	assert.Equal(t, 2, res.PolicyVersion)
	require.Len(t, res.Filtered, 1)
	assert.Equal(t, "app_excluded", res.Filtered[0].Reason)

	// The row stored under v1 still carries policy_version 1.
	var versions []int
	require.NoError(t, database.Select(&versions,
		`SELECT policy_version FROM raw_snapshots ORDER BY id`))
	assert.Equal(t, []int{1, 2}, versions)
}

// --- idempotency ------------------------------------------------------------

func TestIdempotencyAndBatchRollback(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	batch := []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 0), "Terminal", 10),
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 1), "Terminal", 20),
	}
	res, err := ingest.Process(ctx, database, batch)
	require.NoError(t, err)
	assert.Equal(t, 2, res.Accepted)

	// Identical re-submission: processed once, no error.
	res, err = ingest.Process(ctx, database, batch)
	require.NoError(t, err)
	assert.Equal(t, 0, res.Accepted)
	assert.Equal(t, 2, res.Duplicates)
	assert.Equal(t, 2, rawCount(t, database))

	// Conflicting content for an existing key: whole batch rejected,
	// including the brand-new valid snapshot in the same batch.
	conflicting := []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 0), "Terminal", 999), // same key, different content
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 2), "Terminal", 30),   // new, must be rolled back too
	}
	_, err = ingest.Process(ctx, database, conflicting)
	var ce *ingest.ConflictError
	require.ErrorAs(t, err, &ce)
	assert.Equal(t, 2, rawCount(t, database), "conflict must roll back the entire batch")

	// Validation failure also rolls back everything.
	_, err = ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 3), "Terminal", 40),
		snap("ws-ghost", empBob, utc(2026, 1, 5, 10, 4), "Terminal", 50), // unknown workstation
	})
	var ve *ingest.ValidationError
	require.ErrorAs(t, err, &ve)
	assert.Equal(t, 2, rawCount(t, database))
}

// --- cross-day timezone handling ---------------------------------------------

func TestCrossDayTimezone(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	// Widen the window so late-night UTC minutes are monitored.
	_, err := database.Exec(`
		INSERT INTO policies (version, work_start, work_end, excluded_apps)
		VALUES (2, '00:00', '23:59', '[]')`)
	require.NoError(t, err)

	// Alice (UTC+8): 2026-01-05 16:30 UTC is 2026-01-06 00:30 local.
	_, err = ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-alice", empAlice, utc(2026, 1, 5, 16, 30), "Terminal", 8),
	})
	require.NoError(t, err)

	var localDate string
	require.NoError(t, database.Get(&localDate,
		`SELECT to_char(local_date, 'YYYY-MM-DD') FROM raw_snapshots`))
	assert.Equal(t, "2026-01-06", localDate, "local_date must use the employee's timezone")

	_, ok := getDaily(t, database, empAlice, "2026-01-06")
	assert.True(t, ok, "daily summary must be filed under the local date")
	_, ok = getDaily(t, database, empAlice, "2026-01-05")
	assert.False(t, ok)
}

// --- late backfill -----------------------------------------------------------

func TestLateBackfillRecomputesOnlyAffectedDays(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	day1 := []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 0), "Terminal", 10), // productive
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 1), "Slack", 4),      // unproductive
	}
	day2 := []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 6, 10, 0), "Terminal", 20),
	}
	_, err := ingest.Process(ctx, database, day1)
	require.NoError(t, err)
	_, err = ingest.Process(ctx, database, day2)
	require.NoError(t, err)

	before2, ok := getDaily(t, database, empBob, "2026-01-06")
	require.True(t, ok)

	// Late snapshot for Jan 5 arrives a day later.
	_, err = ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 2), "Terminal", 5),
	})
	require.NoError(t, err)

	d1, ok := getDaily(t, database, empBob, "2026-01-05")
	require.True(t, ok)
	assert.Equal(t, int64(15), d1.Prod)
	assert.Equal(t, int64(4), d1.Unprod)
	assert.Equal(t, 3, d1.Snapshots)

	after2, ok := getDaily(t, database, empBob, "2026-01-06")
	require.True(t, ok)
	assert.Equal(t, before2, after2, "unaffected days must not change")

	// Weekly summary reflects the backfill too.
	var weeklyTotal int64
	require.NoError(t, database.Get(&weeklyTotal, `
		SELECT total_count FROM weekly_summaries
		WHERE department_id = $1 AND week_start = '2026-01-05'`, deptEng))
	assert.Equal(t, int64(39), weeklyTotal)
}

// --- classification rule versions ---------------------------------------------

func TestRuleVersionsDoNotRewriteHistory(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	// Rules v1: *slack* is unproductive.
	_, err := ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 0), "Slack", 6),
	})
	require.NoError(t, err)

	// Publish rules v2: Slack becomes productive.
	_, err = database.Exec(`
		INSERT INTO classification_rules (version, pattern, category, priority) VALUES
			(2, '*slack*', 'productive', 10),
			(2, '*code*', 'productive', 10),
			(2, '*terminal*', 'productive', 10)`)
	require.NoError(t, err)

	_, err = ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 1), "Slack", 6),
	})
	require.NoError(t, err)

	type row struct {
		Category     string `db:"category"`
		RulesVersion int    `db:"rules_version"`
	}
	var rows []row
	require.NoError(t, database.Select(&rows,
		`SELECT category, rules_version FROM raw_snapshots ORDER BY minute_utc`))
	require.Len(t, rows, 2)
	assert.Equal(t, row{"unproductive", 1}, rows[0], "historical row keeps its classification")
	assert.Equal(t, row{"productive", 2}, rows[1], "new row uses the new rules version")

	d, ok := getDaily(t, database, empBob, "2026-01-05")
	require.True(t, ok)
	assert.Equal(t, int64(6), d.Prod)
	assert.Equal(t, int64(6), d.Unprod)
}

// --- rebuild consistency and concurrency ---------------------------------------

// independentDaily recomputes what the summary SHOULD be, straight from raw.
func independentDaily(t *testing.T, database *sqlx.DB) map[string]dailySummary {
	t.Helper()
	rows := []struct {
		Emp     int64  `db:"employee_id"`
		Day     string `db:"day"`
		Prod    int64  `db:"prod"`
		Unprod  int64  `db:"unprod"`
		Neutral int64  `db:"neutral"`
		Total   int64  `db:"total"`
		N       int    `db:"n"`
	}{}
	require.NoError(t, database.Select(&rows, `
		SELECT employee_id, to_char(local_date, 'YYYY-MM-DD') AS day,
			COALESCE(SUM(activity_count) FILTER (WHERE category='productive'),0) AS prod,
			COALESCE(SUM(activity_count) FILTER (WHERE category='unproductive'),0) AS unprod,
			COALESCE(SUM(activity_count) FILTER (WHERE category='neutral'),0) AS neutral,
			COALESCE(SUM(activity_count),0) AS total,
			COUNT(*) AS n
		FROM raw_snapshots GROUP BY employee_id, local_date`))
	out := map[string]dailySummary{}
	for _, r := range rows {
		out[fmt.Sprintf("%d|%s", r.Emp, r.Day)] = dailySummary{r.Prod, r.Unprod, r.Neutral, r.Total, r.N}
	}
	return out
}

func storedDaily(t *testing.T, database *sqlx.DB) map[string]dailySummary {
	t.Helper()
	rows := []struct {
		Emp     int64  `db:"employee_id"`
		Day     string `db:"day"`
		Prod    int64  `db:"productive_count"`
		Unprod  int64  `db:"unproductive_count"`
		Neutral int64  `db:"neutral_count"`
		Total   int64  `db:"total_count"`
		N       int    `db:"snapshots"`
	}{}
	require.NoError(t, database.Select(&rows, `
		SELECT employee_id, to_char(day, 'YYYY-MM-DD') AS day,
			productive_count, unproductive_count, neutral_count, total_count, snapshots
		FROM daily_summaries`))
	out := map[string]dailySummary{}
	for _, r := range rows {
		out[fmt.Sprintf("%d|%s", r.Emp, r.Day)] = dailySummary{r.Prod, r.Unprod, r.Neutral, r.Total, r.N}
	}
	return out
}

func TestRebuildMatchesIncrementalAndSurvivesConcurrency(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	// Base load: two employees, three days each. Alice is UTC+8, so her
	// in-window UTC hours are 01:00-09:59; Bob (UTC) uses 10:00.
	minute := 0
	var base []ingest.Snapshot
	for day := 5; day <= 7; day++ {
		for _, ws := range []struct {
			id   string
			emp  int64
			hour int
		}{{"ws-alice", empAlice, 2}, {"ws-bob", empBob, 10}} {
			base = append(base,
				snap(ws.id, ws.emp, utc(2026, 1, day, ws.hour, minute%50), "Terminal", 10),
				snap(ws.id, ws.emp, utc(2026, 1, day, ws.hour, (minute+1)%50), "Slack", 5),
			)
			minute += 2
		}
	}
	_, err := ingest.Process(ctx, database, base)
	require.NoError(t, err)

	// Rebuild from raw must reproduce the incremental results exactly.
	incremental := storedDaily(t, database)
	_, err = aggregate.Rebuild(ctx, database, utc(2026, 1, 1, 0, 0), utc(2026, 1, 31, 0, 0))
	require.NoError(t, err)
	assert.Equal(t, incremental, storedDaily(t, database),
		"rebuild from raw must match incremental processing")

	// Hammer the same dates with concurrent late backfills and rebuilds.
	const workers = 8
	var wg sync.WaitGroup
	errs := make(chan error, workers*40)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < 20; i++ {
				day := 5 + (w+i)%3
				ws, emp, hour := "ws-bob", empBob, 15
				if (w+i)%2 == 0 {
					ws, emp, hour = "ws-alice", empAlice, 5 // 13:00 local for Alice
				}
				// Unique minute per (worker, iteration) so every insert is new
				// and cannot collide with the base load's minutes.
				ts := utc(2026, 1, day, hour, w*20+i)
				_, err := ingest.Process(ctx, database, []ingest.Snapshot{
					snap(ws, int64(emp), ts, "Terminal", 1),
				})
				if err != nil {
					errs <- err
				}
			}
		}(w)
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 5; i++ {
			if _, err := aggregate.Rebuild(ctx, database, utc(2026, 1, 1, 0, 0), utc(2026, 1, 31, 0, 0)); err != nil {
				errs <- err
			}
		}
	}()
	wg.Wait()
	close(errs)
	for err := range errs {
		require.NoError(t, err)
	}

	// After a final rebuild, stored summaries must equal an independent
	// recomputation from raw: no write lost, nothing double-counted.
	_, err = aggregate.Rebuild(ctx, database, utc(2026, 1, 1, 0, 0), utc(2026, 1, 31, 0, 0))
	require.NoError(t, err)
	assert.Equal(t, independentDaily(t, database), storedDaily(t, database))

	expectedRaw := len(base) + workers*20
	assert.Equal(t, expectedRaw, rawCount(t, database))
}

// --- cleanup boundary -----------------------------------------------------------

func TestCleanupBoundary(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	_, err := ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 0), "Terminal", 10),
		snap("ws-bob", empBob, utc(2026, 1, 10, 10, 0), "Terminal", 20),
	})
	require.NoError(t, err)

	// Clean up raw data before Jan 8.
	deleted, err := aggregate.Cleanup(ctx, database, utc(2026, 1, 8, 0, 0))
	require.NoError(t, err)
	assert.Equal(t, int64(1), deleted)
	assert.Equal(t, 1, rawCount(t, database))

	// Summaries survive the cleanup.
	d, ok := getDaily(t, database, empBob, "2026-01-05")
	require.True(t, ok)
	assert.Equal(t, int64(10), d.Total)

	// Rebuildable range is marked.
	retained, err := aggregate.RetainedFrom(ctx, database)
	require.NoError(t, err)
	require.NotNil(t, retained)
	assert.Equal(t, "2026-01-08", retained.Format("2006-01-02"))

	// Reaching into purged history is refused: partial data must never
	// overwrite the complete Jan 5 summary.
	_, err = aggregate.Rebuild(ctx, database, utc(2026, 1, 1, 0, 0), utc(2026, 1, 31, 0, 0))
	require.ErrorIs(t, err, aggregate.ErrRangePurged)
	d, ok = getDaily(t, database, empBob, "2026-01-05")
	require.True(t, ok)
	assert.Equal(t, int64(10), d.Total, "purged-day summary must remain intact")

	// Rebuilding inside the retained range still works and is a no-op here.
	_, err = aggregate.Rebuild(ctx, database, utc(2026, 1, 8, 0, 0), utc(2026, 1, 31, 0, 0))
	require.NoError(t, err)
	d, ok = getDaily(t, database, empBob, "2026-01-10")
	require.True(t, ok)
	assert.Equal(t, int64(20), d.Total)
}

// --- department isolation over HTTP ---------------------------------------------

func TestDepartmentIsolation(t *testing.T) {
	database := testDB(t)
	ctx := context.Background()

	_, err := ingest.Process(ctx, database, []ingest.Snapshot{
		snap("ws-alice", empAlice, utc(2026, 1, 5, 1, 30), "Terminal", 10), // 09:30 local
		snap("ws-bob", empBob, utc(2026, 1, 5, 10, 0), "Terminal", 20),
	})
	require.NoError(t, err)

	e := api.NewServer(database)
	do := func(token, method, path string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, nil)
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		return rec
	}

	q := "?from=2026-01-01&to=2026-01-31"

	// Unauthenticated.
	assert.Equal(t, http.StatusUnauthorized, do("", "GET", "/v1/employees/3/daily"+q).Code)

	// Employee: self only.
	assert.Equal(t, http.StatusOK, do("token-alice", "GET", "/v1/employees/3/daily"+q).Code)
	assert.Equal(t, http.StatusForbidden, do("token-alice", "GET", "/v1/employees/4/daily"+q).Code)

	// Manager: own department only, for reports, detail and export alike.
	assert.Equal(t, http.StatusOK, do("token-mgr", "GET", "/v1/employees/3/daily"+q).Code)
	assert.Equal(t, http.StatusForbidden, do("token-mgr", "GET", "/v1/employees/5/daily"+q).Code)
	assert.Equal(t, http.StatusOK, do("token-mgr", "GET", "/v1/departments/1/weekly"+q).Code)
	assert.Equal(t, http.StatusForbidden, do("token-mgr", "GET", "/v1/departments/2/weekly"+q).Code)
	assert.Equal(t, http.StatusForbidden, do("token-mgr", "GET", "/v1/departments/2/export.csv"+q).Code)

	// Detail endpoint: manager sees only own department's rows.
	rec := do("token-mgr", "GET", "/v1/snapshots"+q)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Contains(t, rec.Body.String(), "ws-alice")
	assert.NotContains(t, rec.Body.String(), "ws-eve")

	// Admin endpoints require the admin role.
	assert.Equal(t, http.StatusForbidden, do("token-mgr", "POST", "/v1/admin/rebuild").Code)
	assert.Equal(t, http.StatusForbidden, do("token-alice", "POST", "/v1/admin/cleanup").Code)
}

func TestIngestHTTP(t *testing.T) {
	database := testDB(t)
	e := api.NewServer(database)

	body := `{"snapshots":[{"workstation_id":"ws-bob","employee_id":4,` +
		`"timestamp":"2026-01-05T10:00:00Z","app_name":"Terminal","activity_count":7}]}`
	req := httptest.NewRequest("POST", "/v1/snapshots", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer token-bob")
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Contains(t, rec.Body.String(), `"accepted":1`)

	// Conflict over HTTP -> 409.
	conflict := `{"snapshots":[{"workstation_id":"ws-bob","employee_id":4,` +
		`"timestamp":"2026-01-05T10:00:00Z","app_name":"Terminal","activity_count":99}]}`
	req = httptest.NewRequest("POST", "/v1/snapshots", strings.NewReader(conflict))
	req.Header.Set("Authorization", "Bearer token-bob")
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusConflict, rec.Code)
}
