package httpapi_test

// HTTP-level tests for authentication and department isolation. Skipped
// unless TEST_DATABASE_URL is set (see internal/store integration tests).

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"

	"desklens/internal/classify"
	"desklens/internal/httpapi"
	"desklens/internal/store"
	"desklens/migrations"
)

func testServer(t *testing.T) *httptest.Server {
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

	if _, err := db.Exec(`
		DROP TABLE IF EXISTS raw_snapshots, employee_daily_summary, department_weekly_summary,
			classification_rules, classification_versions, policy_versions,
			retention_state, users, employees, departments, schema_migrations CASCADE`); err != nil {
		t.Fatalf("reset schema: %v", err)
	}
	if err := migrations.Apply(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if _, err := db.Exec(`
		INSERT INTO departments (id, name) VALUES (1, 'Engineering'), (2, 'Sales');
		INSERT INTO employees (id, department_id, name, timezone) VALUES
			(1, 1, 'Alice', 'UTC'), (2, 2, 'Carol', 'UTC');
		INSERT INTO users (username, token, role, department_id) VALUES
			('admin',      'admin-token',    'admin',   NULL),
			('ingest-svc', 'ingest-token',   'ingest',  NULL),
			('mgr-eng',    'mgr-eng-token',  'manager', 1),
			('mgr-sales',  'mgr-sales-token','manager', 2);
		INSERT INTO policy_versions (work_start_minutes, work_end_minutes, workdays, excluded_apps, exempt_department_ids)
		VALUES (0, 1439, ARRAY[0,1,2,3,4,5,6]::smallint[], '{}', '{}');`); err != nil {
		t.Fatalf("fixtures: %v", err)
	}

	st := store.New(db)
	if _, err := st.PublishClassification(t.Context(), []classify.Rule{
		{Pattern: "*Code*", Category: classify.Productive, Priority: 10},
	}); err != nil {
		t.Fatalf("classification: %v", err)
	}

	e := httpapi.Router(st)
	srv := httptest.NewServer(e)
	t.Cleanup(srv.Close)

	// One snapshot for Alice (dept 1) so there is data to protect.
	req, err := http.NewRequest(http.MethodPost, srv.URL+"/api/v1/snapshots/batch",
		strings.NewReader(`{"snapshots":[{"workstation_id":"ws1","employee_id":1,
			"captured_at":"2026-09-14T10:00:00Z","app_name":"Code","activity_count":7}]}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer ingest-token")
	req.Header.Set("Content-Type", "application/json")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusOK {
		t.Fatalf("ingest status = %d", res.StatusCode)
	}
	return srv
}

func do(t *testing.T, method, url, token string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(method, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { res.Body.Close() })
	return res
}

func TestAuthAndDepartmentIsolation(t *testing.T) {
	srv := testServer(t)
	base := srv.URL

	cases := []struct {
		name   string
		url    string
		token  string
		status int
	}{
		{"no token is rejected", base + "/api/v1/employees/1/daily?from=2026-09-01&to=2026-09-20", "", 401},
		{"bad token is rejected", base + "/api/v1/employees/1/daily?from=2026-09-01&to=2026-09-20", "nope", 401},
		{"admin reads any employee", base + "/api/v1/employees/1/daily?from=2026-09-01&to=2026-09-20", "admin-token", 200},
		{"manager reads own department", base + "/api/v1/employees/1/daily?from=2026-09-01&to=2026-09-20", "mgr-eng-token", 200},
		{"manager cannot read other department employee", base + "/api/v1/employees/2/daily?from=2026-09-01&to=2026-09-20", "mgr-eng-token", 403},
		{"manager cannot read other department weekly", base + "/api/v1/departments/2/weekly?from=2026-09-01&to=2026-09-30", "mgr-eng-token", 403},
		{"manager reads own department weekly", base + "/api/v1/departments/1/weekly?from=2026-09-01&to=2026-09-30", "mgr-eng-token", 200},
		{"export is isolated too", base + "/api/v1/export/employees/2/daily.csv?from=2026-09-01&to=2026-09-20", "mgr-eng-token", 403},
		{"export own department works", base + "/api/v1/export/employees/1/daily.csv?from=2026-09-01&to=2026-09-20", "mgr-eng-token", 200},
		{"detail endpoint is isolated", base + "/api/v1/employees/2/snapshots?date=2026-09-14", "mgr-eng-token", 403},
		{"ingest token cannot read summaries", base + "/api/v1/employees/1/daily?from=2026-09-01&to=2026-09-20", "ingest-token", 403},
		{"manager cannot publish policy", base + "/api/v1/admin/policies", "mgr-eng-token", 403},
		{"manager cannot trigger cleanup", base + "/api/v1/admin/cleanup", "mgr-eng-token", 403},
	}
	for _, c := range cases {
		res := do(t, http.MethodGet, c.url, c.token)
		if res.StatusCode != c.status {
			t.Errorf("%s: status = %d, want %d", c.name, res.StatusCode, c.status)
		}
	}
}

func TestIngestValidationAndConflictOverHTTP(t *testing.T) {
	srv := testServer(t)

	post := func(body string) *http.Response {
		req, _ := http.NewRequest(http.MethodPost, srv.URL+"/api/v1/snapshots/batch",
			strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer ingest-token")
		req.Header.Set("Content-Type", "application/json")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { res.Body.Close() })
		return res
	}

	// Validation failure rolls back the whole batch.
	res := post(`{"snapshots":[
		{"workstation_id":"ws9","employee_id":1,"captured_at":"2026-09-15T10:00:00Z","app_name":"Code","activity_count":3},
		{"workstation_id":"ws9","employee_id":1,"captured_at":"2026-09-15T10:01:00Z","app_name":"Code","activity_count":-1}]}`)
	if res.StatusCode != http.StatusBadRequest {
		t.Fatalf("invalid batch: status = %d, want 400", res.StatusCode)
	}

	// Same content again -> duplicate, still 200.
	good := `{"snapshots":[{"workstation_id":"ws9","employee_id":1,"captured_at":"2026-09-15T10:00:00Z","app_name":"Code","activity_count":3}]}`
	if res := post(good); res.StatusCode != http.StatusOK {
		t.Fatalf("first delivery: status = %d", res.StatusCode)
	}
	if res := post(good); res.StatusCode != http.StatusOK {
		t.Fatalf("identical re-delivery: status = %d", res.StatusCode)
	}
	// Conflicting content for the same key -> 409.
	res = post(`{"snapshots":[{"workstation_id":"ws9","employee_id":1,"captured_at":"2026-09-15T10:00:00Z","app_name":"Code","activity_count":99}]}`)
	if res.StatusCode != http.StatusConflict {
		t.Fatalf("conflicting re-delivery: status = %d, want 409", res.StatusCode)
	}
}
