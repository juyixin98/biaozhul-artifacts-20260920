package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"sitevitals/internal/api"
	"sitevitals/internal/models"
	"sitevitals/internal/testutil"
	"sitevitals/internal/whitelist"
)

func newRouter(t *testing.T) (*httptest.Server, func()) {
	st := testutil.NewStore(t)
	ctx := context.Background()
	if _, err := st.EnsureSite(ctx, "demo", "http://demo.local:8090", "/", true); err != nil {
		t.Fatal(err)
	}
	matcher := func() *whitelist.Matcher {
		sites, _ := st.ListSites(ctx, false)
		return whitelist.NewMatcher(sites)
	}
	r := api.NewServer(st, matcher).Router()
	ts := httptest.NewServer(r)
	return ts, ts.Close
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(ts.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}

func getJSON(t *testing.T, ts *httptest.Server, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}

func decode(t *testing.T, resp *http.Response) (int, map[string]any) {
	t.Helper()
	out := map[string]any{}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestEnqueue_Validation(t *testing.T) {
	if testing.Short() {
		t.Skip("db test")
	}
	ts, closeFn := newRouter(t)
	defer closeFn()

	// Non-HTTP scheme -> 403 policy error.
	code, body := postJSON(t, ts, "/api/tasks", map[string]any{"url": "file:///etc/passwd", "viewport": "desktop"})
	if code != http.StatusForbidden {
		t.Fatalf("file: status=%d body=%v", code, body)
	}
	// Whitelisted host works.
	code, body = postJSON(t, ts, "/api/tasks", map[string]any{"url": "http://demo.local:8090/normal", "viewport": "desktop"})
	if code != http.StatusCreated {
		t.Fatalf("whitelisted enqueue: status=%d body=%v", code, body)
	}
	if body["status"] != models.StateQueued {
		t.Fatalf("new task status = %v", body["status"])
	}
	// Off-whitelist http target -> 403.
	code, _ = postJSON(t, ts, "/api/tasks", map[string]any{"url": "http://evil.invalid/", "viewport": "desktop"})
	if code != http.StatusForbidden {
		t.Fatalf("off-list enqueue status=%d, want 403", code)
	}
	// Bad viewport -> 400.
	code, _ = postJSON(t, ts, "/api/tasks", map[string]any{"url": "http://demo.local:8090/", "viewport": "tv"})
	if code != http.StatusBadRequest {
		t.Fatalf("bad viewport status=%d, want 400", code)
	}
}

func TestSiteCRUD_AndEnqueueUsesUpdatedWhitelist(t *testing.T) {
	if testing.Short() {
		t.Skip("db test")
	}
	ts, closeFn := newRouter(t)
	defer closeFn()

	code, _ := postJSON(t, ts, "/api/sites", map[string]any{
		"name": "intra", "origin": "http://intra.example.com", "path_prefix": "/apps/",
	})
	if code != http.StatusCreated {
		t.Fatalf("create site status=%d", code)
	}
	code, body := postJSON(t, ts, "/api/tasks", map[string]any{
		"url": "http://intra.example.com/apps/dashboard", "viewport": "mobile",
	})
	if code != http.StatusCreated {
		t.Fatalf("enqueue under prefix: %d %v", code, body)
	}
	code, _ = postJSON(t, ts, "/api/tasks", map[string]any{
		"url": "http://intra.example.com/root", "viewport": "mobile",
	})
	if code != http.StatusForbidden {
		t.Fatalf("path outside prefix status=%d, want 403", code)
	}
}

func TestHealthz(t *testing.T) {
	if testing.Short() {
		t.Skip("db test")
	}
	ts, closeFn := newRouter(t)
	defer closeFn()
	code, body := getJSON(t, ts, "/healthz")
	if code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("healthz %d %v", code, body)
	}
}

func TestGlobalBudgetCRUD(t *testing.T) {
	if testing.Short() {
		t.Skip("db test")
	}
	ts, closeFn := newRouter(t)
	defer closeFn()
	code, body := getJSON(t, ts, "/api/budgets/global")
	if code != http.StatusOK {
		t.Fatalf("get budget: %d %v", code, body)
	}
	// Defaults seeded.
	if body["fcp_ms"] == nil {
		t.Fatal("default FCP budget not seeded")
	}
	code, body = putJSON(t, ts, "/api/budgets/global", map[string]any{"fcp_ms": 1234.5})
	if code != http.StatusOK {
		t.Fatalf("put budget: %d %v", code, body)
	}
}

func putJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPut, ts.URL+path, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}
