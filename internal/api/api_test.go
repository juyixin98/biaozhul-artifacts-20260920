package api_test

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"costlens/internal/api"
	"costlens/internal/service"
	"costlens/internal/testdb"
)

var (
	pool *pgxpool.Pool
	svc  *service.Service
	ts   *httptest.Server
	ctxb = context.Background()
)

func TestMain(m *testing.M) {
	var err error
	var cleanup func()
	pool, cleanup, err = testdb.Setup("api")
	if err != nil {
		fmt.Fprintln(os.Stderr, "integration tests skipped:", err)
		os.Exit(0)
	}
	defer cleanup()
	svc = service.New(pool)
	ts = httptest.NewServer(api.NewRouter(svc))
	defer ts.Close()
	os.Exit(m.Run())
}

func reset(t *testing.T) {
	t.Helper()
	_, err := pool.Exec(ctxb, `TRUNCATE TABLE
		rebuild_events, anomaly_evaluations, budget_alerts, budgets,
		import_batches, billing_records, daily_summaries, monthly_summaries,
		user_org_grants, users, accounts, cost_centers,
		organizations RESTART IDENTITY CASCADE`)
	if err != nil {
		t.Fatal(err)
	}
}

func mkOrg(t *testing.T, ext string) {
	t.Helper()
	if _, err := svc.CreateOrg(ctxb, service.CreateOrgInput{ExternalID: ext, Name: ext}); err != nil {
		t.Fatal(err)
	}
}

func mkCCAcct(t *testing.T, org, cc, acct string) {
	t.Helper()
	if _, err := svc.CreateCostCenter(ctxb,
		service.CreateCostCenterInput{OrgExternalID: org, Code: cc, Name: cc}); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.CreateAccount(ctxb, service.CreateAccountInput{
		OrgExternalID: org, CostCenterCode: cc, ExternalID: acct, Name: acct}); err != nil {
		t.Fatal(err)
	}
}

func mkUser(t *testing.T, name, role string) string {
	t.Helper()
	_, tok, err := svc.CreateUser(ctxb, service.CreateUserInput{Username: name, Role: role})
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func grant(t *testing.T, user, org string) {
	t.Helper()
	if err := svc.Grant(ctxb, user, org); err != nil {
		t.Fatal(err)
	}
}

func doReq(t *testing.T, method, path, token, contentType string, body []byte) (int, []byte) {
	t.Helper()
	req, err := http.NewRequest(method, ts.URL+path, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b
}

func csvLine(r, s, day, ccy, amt string) string {
	return r + "," + s + "," + day + "," + ccy + "," + amt + "\n"
}

func TestAuthAndIsolation(t *testing.T) {
	reset(t)
	mkOrg(t, "org-a")
	mkOrg(t, "org-b")
	mkCCAcct(t, "org-a", "cca", "acct-a")
	mkCCAcct(t, "org-b", "ccb", "acct-b")

	adminTok := mkUser(t, "root", "admin")
	analystA := mkUser(t, "ana", "analyst")
	viewerA := mkUser(t, "view", "viewer")
	analystB := mkUser(t, "anb", "analyst")
	grant(t, "ana", "org-a")
	grant(t, "view", "org-a")
	grant(t, "anb", "org-b")

	// No token -> 401.
	if code, _ := doReq(t, "GET", "/api/v1/accounts", "", "", nil); code != 401 {
		t.Fatalf("no token: want 401, got %d", code)
	}

	// Viewer cannot import -> 403.
	csv := "resource_id,service,date,currency,amount\n" +
		csvLine("r1", "compute", "2026-09-01", "USD", "1")
	if code, _ := doReq(t, "POST", "/api/v1/accounts/acct-a/imports",
		viewerA, "text/csv", []byte(csv)); code != 403 {
		t.Fatalf("viewer import: want 403, got %d", code)
	}

	// Analyst A cannot import into org B -> 403.
	if code, _ := doReq(t, "POST", "/api/v1/accounts/acct-b/imports",
		analystA, "text/csv", []byte(csv)); code != 403 {
		t.Fatalf("cross-org import: want 403, got %d", code)
	}

	// Viewer cannot rebuild -> 403.
	if code, _ := doReq(t, "POST", "/api/v1/admin/rebuild",
		viewerA, "application/json", nil); code != 403 {
		t.Fatalf("viewer rebuild: want 403, got %d", code)
	}

	// Analyst A imports into org A -> 200.
	code, body := doReq(t, "POST", "/api/v1/accounts/acct-a/imports",
		analystA, "text/csv", []byte(csv))
	if code != 200 {
		t.Fatalf("authorized import: want 200, got %d: %s", code, body)
	}

	// Analyst B imports a distinct row into org B.
	csvB := "resource_id,service,date,currency,amount\n" +
		csvLine("rB", "compute", "2026-09-01", "USD", "99")
	if code, body := doReq(t, "POST", "/api/v1/accounts/acct-b/imports",
		analystB, "text/csv", []byte(csvB)); code != 200 {
		t.Fatalf("org-b import: %d %s", code, body)
	}

	// Viewer A sees only the org-A row.
	code, body = doReq(t, "GET", "/api/v1/records?limit=100", viewerA, "", nil)
	if code != 200 {
		t.Fatalf("records: %d", code)
	}
	if bytes.Contains(body, []byte("rB")) {
		t.Fatalf("viewer must not see org-b rows: %s", body)
	}
	if !bytes.Contains(body, []byte("r1")) {
		t.Fatalf("viewer must see org-a row: %s", body)
	}

	// Analyst B sees only rB.
	_, body = doReq(t, "GET", "/api/v1/records?limit=100", analystB, "", nil)
	if bytes.Contains(body, []byte(`"r1"`)) || !bytes.Contains(body, []byte("rB")) {
		t.Fatalf("analyst B isolation broken: %s", body)
	}

	// Admin sees both.
	_, body = doReq(t, "GET", "/api/v1/records?limit=100", adminTok, "", nil)
	if !bytes.Contains(body, []byte("rB")) || !bytes.Contains(body, []byte(`"r1"`)) {
		t.Fatalf("admin must see all rows: %s", body)
	}

	// CSV export isolation: viewer A export must not contain rB.
	_, body = doReq(t, "GET", "/api/v1/exports/records.csv", viewerA, "", nil)
	if bytes.Contains(body, []byte("rB")) || !bytes.Contains(body, []byte("acct-a")) {
		t.Fatalf("export isolation broken: %s", body)
	}

	// Summaries: viewer A cost-center scope cannot see org-B center totals.
	_, body = doReq(t, "GET", "/api/v1/summaries/daily?scope=cost_center", viewerA, "", nil)
	if !bytes.Contains(body, []byte("1.000000")) {
		t.Fatalf("viewer should see own cc total: %s", body)
	}
	// Two visible orgs for admin -> admin cc summary contains 99 and 1.
	_, body = doReq(t, "GET", "/api/v1/summaries/daily?scope=cost_center", adminTok, "", nil)
	if !bytes.Contains(body, []byte("99.000000")) {
		t.Fatalf("admin cc summary should include org-b total: %s", body)
	}
}

// Bad CSV returns 422 with a line number and imports nothing.
func TestCSVValidation422(t *testing.T) {
	reset(t)
	mkOrg(t, "org-x")
	mkCCAcct(t, "org-x", "ccx", "acct-x")
	analyst := mkUser(t, "anx", "analyst")
	grant(t, "anx", "org-x")

	bad := "resource_id,service,date,currency,amount\n" +
		csvLine("ok", "compute", "2026-09-01", "USD", "1") +
		csvLine("bad", "compute", "not-a-date", "USD", "2")
	code, body := doReq(t, "POST", "/api/v1/accounts/acct-x/imports",
		analyst, "text/csv", []byte(bad))
	if code != 422 {
		t.Fatalf("want 422, got %d: %s", code, body)
	}
	if !bytes.Contains(body, []byte(`"line":3`)) {
		t.Fatalf("error must cite line 3: %s", body)
	}
	var n int
	if err := pool.QueryRow(ctxb,
		`SELECT count(*) FROM billing_records`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("validation failure must import nothing, n=%d", n)
	}
}
