package integrationtest

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"desklens/internal/repo"
)

func getJSON(t *testing.T, url, token string, target any) int {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if target != nil {
		json.NewDecoder(res.Body).Decode(target)
	}
	return res.StatusCode
}

// Manager access is scoped to their own department on summaries, raw detail
// and CSV export; cross-department access and anonymous access are rejected.
func TestManagerScopeIsolation(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
		snap(1003, 103, "2026-03-10T18:00:00Z", "Zoom Meetings", 30),
	}); err != nil {
		t.Fatal(err)
	}
	base := env.Server.URL

	// Engineering manager sees Ada only.
	var daily struct {
		Daily []repo.DailySummary `json:"daily"`
	}
	if code := getJSON(t, base+"/api/v1/summaries/daily", "manager-eng-token", &daily); code != 200 {
		t.Fatalf("status %d", code)
	}
	if len(daily.Daily) != 1 || daily.Daily[0].EmployeeID != 101 {
		t.Fatalf("eng manager saw %+v", daily.Daily)
	}

	// Detail endpoint: sales manager sees Grace only.
	var act struct {
		Activity []repo.RawSnapshot `json:"activity"`
	}
	if code := getJSON(t, base+"/api/v1/activity", "manager-sales-token", &act); code != 200 {
		t.Fatalf("status %d", code)
	}
	for _, r := range act.Activity {
		if r.DepartmentID != 2 {
			t.Fatalf("sales manager saw department %d", r.DepartmentID)
		}
	}

	// CSV export applies the same scope.
	req, _ := http.NewRequest(http.MethodGet, base+"/api/v1/export/activity.csv", nil)
	req.Header.Set("Authorization", "Bearer manager-sales-token")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	buf := new(bytes.Buffer)
	buf.ReadFrom(res.Body)
	csv := buf.String()
	if strings.Contains(csv, "VS Code") || strings.Contains(csv, ",101,") {
		t.Fatalf("export leaked engineering rows:\n%s", csv)
	}
	if !strings.Contains(csv, "Zoom Meetings") {
		t.Fatalf("export missing own department rows:\n%s", csv)
	}

	// Explicit foreign department id -> 403.
	if code := getJSON(t, base+"/api/v1/summaries/daily?department_id=2", "manager-eng-token", nil); code != http.StatusForbidden {
		t.Fatalf("cross-dept status = %d, want 403", code)
	}
	// Anonymous -> 401.
	if code := getJSON(t, base+"/api/v1/summaries/daily", "", nil); code != http.StatusUnauthorized {
		t.Fatalf("anonymous status = %d, want 401", code)
	}
	// Manager cannot rebuild.
	req2, _ := http.NewRequest(http.MethodPost, base+"/api/v1/rebuild", strings.NewReader(`{"scope":"all"}`))
	req2.Header.Set("Authorization", "Bearer manager-eng-token")
	req2.Header.Set("Content-Type", "application/json")
	res2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	defer res2.Body.Close()
	if res2.StatusCode != http.StatusForbidden {
		t.Fatalf("manager rebuild status = %d, want 403", res2.StatusCode)
	}
}
