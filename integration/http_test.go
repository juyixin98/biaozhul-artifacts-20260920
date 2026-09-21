package integration

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"desklens/internal/api"
	"desklens/internal/testsupport"
)

func newHTTPServer(e *env) (*httptest.Server, http.Client) {
	router := api.NewRouter(api.Handlers{
		Ingest:  e.Ingest,
		Manager: e.Manager,
		Admin:   e.Admin,
	}, "admin-test-key")
	srv := httptest.NewServer(router)
	e.t.Cleanup(srv.Close)
	return srv, http.Client{}
}

func reqJSON(t *testing.T, client http.Client, method, url, key string, body string) (int, map[string]any) {
	t.Helper()
	r, err := http.NewRequest(method, url, bytes.NewBufferString(body))
	if err != nil {
		t.Fatal(err)
	}
	if key != "" {
		r.Header.Set("X-API-Key", key)
	}
	r.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	dec := json.NewDecoder(resp.Body)
	_ = dec.Decode(&parsed)
	return resp.StatusCode, parsed
}

// TestHTTPAuthAndRouting verifies each route class requires its own key type.
func TestHTTPAuthAndRouting(t *testing.T) {
	e := setup(t)
	srv, client := newHTTPServer(e)

	// No key -> 401 everywhere.
	status, _ := reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/snapshots", "", `{"snapshots":[]}`)
	if status != http.StatusUnauthorized {
		t.Fatalf("no-key ingest status = %d", status)
	}
	status, _ = reqJSON(t, client, http.MethodGet, srv.URL+"/api/v1/manager/daily?from=2026-09-15&to=2026-09-15", "", "")
	if status != http.StatusUnauthorized {
		t.Fatalf("no-key manager status = %d", status)
	}
	status, _ = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/admin/rebuild", "", `{}`)
	if status != http.StatusUnauthorized {
		t.Fatalf("no-key admin status = %d", status)
	}

	// Key class cannot cross routes: an ingest key is not a manager key.
	status, _ = reqJSON(t, client, http.MethodGet,
		srv.URL+"/api/v1/manager/daily?from=2026-09-15&to=2026-09-15", "ingest-ws101", "")
	if status != http.StatusUnauthorized {
		t.Fatalf("ingest key on manager route = %d", status)
	}
	status, _ = reqJSON(t, client, http.MethodPost,
		srv.URL+"/api/v1/admin/rebuild", "mgr-eng", `{"start_date":"2026-09-15","end_date":"2026-09-15"}`)
	if status != http.StatusUnauthorized {
		t.Fatalf("manager key on admin route = %d", status)
	}

	// Healthz is open.
	resp, err := client.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz = %d", resp.StatusCode)
	}
}

// TestHTTPEndToEndFlow runs ingest -> manager daily -> CSV export over HTTP and
// confirms conflict responses, validation rollback and export isolation.
func TestHTTPEndToEndFlow(t *testing.T) {
	e := setup(t)
	srv, client := newHTTPServer(e)

	body := `{"batch_id":"4e8e9d2c-0000-4000-8000-000000000001","snapshots":[
		{"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:00:00Z","app_name":"Chrome","activity_count":10},
		{"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:01:00Z","app_name":"WeChat","activity_count":4}]}`

	status, out := reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/snapshots", "ingest-ws101", body)
	if status != http.StatusOK {
		t.Fatalf("ingest status = %d body=%v", status, out)
	}
	if acc, _ := out["accepted"].(float64); acc != 2 {
		t.Fatalf("accepted = %v", out["accepted"])
	}

	// Filtered rows over HTTP (1Password excluded).
	body = `{"snapshots":[
		{"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T15:00:00Z","app_name":"1Password","activity_count":1}]}`
	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/snapshots", "ingest-ws101", body)
	if status != http.StatusOK || out["filtered"].(float64) != 1 {
		t.Fatalf("filtered response = %d %v", status, out)
	}

	// Conflict re-send -> 409 snapshot_conflict, whole batch rejected.
	body = `{"snapshots":[
		{"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:00:00Z","app_name":"Chrome","activity_count":999},
		{"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:05:00Z","app_name":"Firefox","activity_count":1}]}`
	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/snapshots", "ingest-ws101", body)
	if status != http.StatusConflict {
		t.Fatalf("conflict status = %d", status)
	}
	if errObj, _ := out["error"].(map[string]any); errObj["code"] != "snapshot_conflict" {
		t.Fatalf("error body = %v", out)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 2 {
		t.Fatalf("conflict batch partially wrote: %d rows", n)
	}

	// Validation failure -> 400, whole batch rejected.
	body = `{"snapshots":[
		{"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T16:00:00Z","app_name":"X","activity_count":-1}]}`
	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/snapshots", "ingest-ws101", body)
	if status != http.StatusBadRequest {
		t.Fatalf("invalid status = %d", status)
	}

	// Manager daily via HTTP.
	status, out = reqJSON(t, client, http.MethodGet,
		srv.URL+"/api/v1/manager/daily?from=2026-09-15&to=2026-09-15", "mgr-eng", "")
	if status != http.StatusOK {
		t.Fatalf("daily status = %d %v", status, out)
	}
	daily, _ := out["daily"].([]any)
	if len(daily) != 1 {
		t.Fatalf("daily rows = %v", daily)
	}
	row := daily[0].(map[string]any)
	if row["total_count"].(float64) != 14 {
		t.Fatalf("daily total = %v", row["total_count"])
	}

	// CSV export: same department key, includes version columns and only raw
	// rows that survived filtering.
	csvReq, _ := http.NewRequest(http.MethodGet,
		srv.URL+"/api/v1/manager/employees/101/export?from=2026-09-15T00:00:00Z&to=2026-09-16T00:00:00Z", nil)
	csvReq.Header.Set("X-API-Key", "mgr-eng")
	resp, err := client.Do(csvReq)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("export status = %d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/csv") {
		t.Fatalf("export content-type = %s", ct)
	}
	r := csv.NewReader(resp.Body)
	records, err := r.ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 3 { // header + 2 raw rows (1Password filtered out)
		t.Fatalf("export rows = %d (%v), want 3", len(records), records)
	}
	if records[0][0] != "bucket_time_utc" || records[0][7] != "policy_version" {
		t.Fatalf("csv header = %v", records[0])
	}
	for _, rec := range records[1:] {
		if rec[3] == "1Password" {
			t.Fatal("excluded app present in export")
		}
	}

	// Export isolation: sales key cannot export engineering employee.
	csvReq, _ = http.NewRequest(http.MethodGet,
		srv.URL+"/api/v1/manager/employees/101/export?from=2026-09-15T00:00:00Z&to=2026-09-16T00:00:00Z", nil)
	csvReq.Header.Set("X-API-Key", "mgr-sales")
	resp, err = client.Do(csvReq)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("cross-dept export = %d, want 404", resp.StatusCode)
	}
}

// TestHTTPAdminPublishAndRebuild covers the admin route happy paths over HTTP.
func TestHTTPAdminPublishAndRebuild(t *testing.T) {
	e := setup(t)
	srv, client := newHTTPServer(e)

	status, out := reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/admin/policy", "admin-test-key", `{
		"monitoring_start":"00:00","monitoring_end":"24:00",
		"excluded_apps":["1password*"],"exempt_departments":[4]}`)
	if status != http.StatusCreated {
		t.Fatalf("publish policy = %d %v", status, out)
	}
	if v, _ := out["version"].(float64); v != 2 {
		t.Fatalf("policy version = %v", out)
	}

	// Invalid window -> 400.
	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/admin/policy", "admin-test-key", `{
		"monitoring_start":"09:00","monitoring_end":"09:00","excluded_apps":["x"]}`)
	if status != http.StatusBadRequest {
		t.Fatalf("bad window = %d %v", status, out)
	}

	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/admin/classification", "admin-test-key", `{
		"rules":[{"rule_id":"r1","pattern":"*","category":"neutral","priority":0}]}`)
	if status != http.StatusCreated || out["version"].(float64) != 2 {
		t.Fatalf("publish classification = %d %v", status, out)
	}

	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/admin/rebuild", "admin-test-key",
		`{"start_date":"2026-09-15","end_date":"2026-09-15"}`)
	if status != http.StatusOK {
		t.Fatalf("rebuild = %d %v", status, out)
	}

	status, out = reqJSON(t, client, http.MethodPost, srv.URL+"/api/v1/admin/retention/purge", "wrong",
		`{"cutoff":"2026-09-15T00:00:00Z"}`)
	if status != http.StatusUnauthorized {
		t.Fatalf("wrong admin key = %d", status)
	}
}
