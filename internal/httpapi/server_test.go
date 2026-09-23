package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"twap/internal/httpapi"
	"twap/internal/service"
	"twap/internal/storage"
)

func setup(t *testing.T) (string, func()) {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		url = "postgres://twap_p015_a:twap_p015_a_pw@localhost:5432/twap_p015_a_http?sslmode=disable"
	}
	ctx := context.Background()
	db, err := storage.New(ctx, url)
	if err != nil {
		t.Skipf("postgresql unavailable: %v", err)
	}
	if err := db.EnsureSchema(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Pool.Exec(ctx, `TRUNCATE samples, window_versions`); err != nil {
		t.Fatal(err)
	}
	svc := service.New(db, service.Config{
		WindowMicros:     60_000_000,
		MaxLateMicros:    5 * 60_000_000,
		StaleAfterMicros: 30_000_000,
		SigningKey:       []byte("httpapi-test-signing-key-32bytes"),
	}, nil)
	handler := httpapi.NewRouter(svc, "test-admin-token")
	ts := httptest.NewServer(handler)
	t.Cleanup(func() {
		ts.Close()
		_, _ = db.Pool.Exec(ctx, `TRUNCATE samples, window_versions`)
		db.Close()
	})
	return ts.URL, func() {}
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Buffer
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewBuffer(b)
	} else {
		rdr = bytes.NewBuffer(nil)
	}
	req, _ := http.NewRequest(method, url, rdr)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestBatchAcceptsMicroAndRFC3339Timestamps(t *testing.T) {
	base, _ := setup(t)
	// Future integer timestamp: rejected with 422 per-item.
	// RFC3339 timestamp in the past beyond the 5-minute cutoff: 422 too.
	// A valid near-now sample is accepted.
	nowMicros := time.Now().UnixMicro()
	status, body := doJSON(t, "POST", base+"/v1/samples", map[string]any{
		"batch": []map[string]any{
			{"ts": nowMicros + 600_000_000, "price": 1, "source": "a"},
			{"ts": "2000-01-01T00:00:00Z", "price": 1, "source": "a"},
			{"ts": nowMicros - 5_000_000, "price": 42, "source": "a"},
		},
	})
	if status != 200 {
		t.Fatalf("batch status = %d, body=%v", status, body)
	}
	if body["accepted"].(float64) != 1 || body["rejected"].(float64) != 2 {
		t.Fatalf("accepted/rejected = %v/%v, want 1/2", body["accepted"], body["rejected"])
	}
	items := body["items"].([]any)
	if items[0].(map[string]any)["status"].(float64) != 422 {
		t.Fatal("future item must be 422")
	}
	if items[1].(map[string]any)["status"].(float64) != 422 {
		t.Fatal("ancient RFC3339 item must be 422")
	}
	if items[2].(map[string]any)["status"].(float64) != 200 {
		t.Fatal("near-now item must be accepted")
	}
}

func TestHealthAndNotFound(t *testing.T) {
	base, _ := setup(t)
	resp, err := http.Get(base + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("health = %d", resp.StatusCode)
	}
	resp2, _ := http.Get(base + "/does-not-exist")
	if resp2.StatusCode != 404 {
		t.Fatalf("missing route = %d, want 404", resp2.StatusCode)
	}
}

func TestAdminAuth(t *testing.T) {
	base, _ := setup(t)
	// No token.
	resp, _ := http.Post(base+"/v1/admin/recompute", "application/json", nil)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no-token status = %d, want 401", resp.StatusCode)
	}
	// Wrong token.
	req, _ := http.NewRequest("POST", base+"/v1/admin/recompute", nil)
	req.Header.Set("Authorization", "Bearer wrong")
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong-token status = %d, want 401", resp.StatusCode)
	}
	// Correct token.
	req, _ = http.NewRequest("POST", base+"/v1/admin/recompute", nil)
	req.Header.Set("Authorization", "Bearer test-admin-token")
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != 200 {
		t.Fatalf("valid token status = %d, want 200", resp.StatusCode)
	}
}

func TestBadTimestampRejected(t *testing.T) {
	base, _ := setup(t)
	status, body := doJSON(t, "POST", base+"/v1/samples", map[string]any{
		"ts": "not-a-time", "price": 1, "source": "a",
	})
	if status != 200 {
		t.Fatalf("status = %d", status)
	}
	if body["rejected"].(float64) != 1 {
		t.Fatalf("rejected = %v, want 1", body["rejected"])
	}
}

func TestRangeValidation(t *testing.T) {
	base, _ := setup(t)
	resp, _ := http.Get(base + "/v1/range?start=100&end=50")
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("inverted range = %d, want 400", resp.StatusCode)
	}
	resp, _ = http.Get(base + "/v1/range?start=abc&end=100")
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("garbage start = %d, want 400", resp.StatusCode)
	}
}
