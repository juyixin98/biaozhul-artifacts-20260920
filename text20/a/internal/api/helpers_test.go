package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/config"
	"signalboard/internal/pgdb"
)

const testAdminToken = "test-admin-token"

type testEnv struct {
	t      *testing.T
	pool   *pgxpool.Pool
	srv    *httptest.Server
	client *http.Client
	seq    int64
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()

	url := envOr("TEST_DATABASE_URL",
		"postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable")

	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Skipf("no database available: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("no database available: %v", err)
	}
	t.Cleanup(pool.Close)

	if _, err := pgdb.Migrate(context.Background(), pool); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	cfg := config.Config{
		Addr:               ":0",
		DatabaseURL:        url,
		AdminToken:         testAdminToken,
		ScreenOfflineAfter: 90 * time.Second,
		ShutdownTimeout:    5 * time.Second,
	}
	handler := NewServer(pool, cfg).Router()
	ts := httptest.NewServer(handler)
	t.Cleanup(ts.Close)

	return &testEnv{
		t:      t,
		pool:   pool,
		srv:    ts,
		client: &http.Client{Timeout: 15 * time.Second},
	}
}

func envOr(k, def string) string {
	if v, ok := os.LookupEnv(k); ok && v != "" {
		return v
	}
	return def
}

// createStoreViaAPI makes a uniquely named store and schedules cascade cleanup.
func (e *testEnv) createStoreViaAPI(tz string) int64 {
	e.t.Helper()
	n := atomic.AddInt64(&e.seq, 1)
	body := fmt.Sprintf(`{"name":"test-%d-%d","timezone":%q}`, time.Now().UnixNano(), n, tz)
	st, data := e.admin(http.MethodPost, "/v1/admin/stores", body)
	if st != http.StatusCreated {
		e.t.Fatalf("create store: status %d body %v", st, data)
	}
	id := int64(data["id"].(float64))
	e.t.Cleanup(func() {
		_, _ = e.pool.Exec(context.Background(), "DELETE FROM stores WHERE id=$1", id)
	})
	return id
}

func (e *testEnv) admin(method, path, body string) (int, map[string]any) {
	st, _, raw, err := e.requestBytes(method, path, map[string]string{
		"Authorization": "Bearer " + testAdminToken,
	}, body)
	if err != nil {
		e.t.Fatalf("%s %s: %v", method, path, err)
	}
	var data map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &data)
	}
	return st, data
}

// adminAny returns the decoded body as any (for JSON array responses).
func (e *testEnv) adminAny(method, path, body string) (int, any) {
	st, _, raw, err := e.requestBytes(method, path, map[string]string{
		"Authorization": "Bearer " + testAdminToken,
	}, body)
	if err != nil {
		e.t.Fatalf("%s %s: %v", method, path, err)
	}
	var data any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &data)
	}
	return st, data
}

// adminSafe is like admin but never calls t.Fatalf, so it is safe to invoke
// from a goroutine (concurrent tests). The caller inspects the error.
func (e *testEnv) adminSafe(method, path, body string) (int, map[string]any, error) {
	st, _, raw, err := e.requestBytes(method, path, map[string]string{
		"Authorization": "Bearer " + testAdminToken,
	}, body)
	if err != nil {
		return st, nil, err
	}
	var data map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &data)
	}
	return st, data, nil
}

func (e *testEnv) screen(token, method, path string, headers map[string]string) (int, http.Header, map[string]any) {
	h := map[string]string{"X-Screen-Token": token}
	for k, v := range headers {
		h[k] = v
	}
	st, hd, raw, err := e.requestBytes(method, path, h, "")
	if err != nil {
		e.t.Fatalf("%s %s: %v", method, path, err)
	}
	var data map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &data)
	}
	return st, hd, data
}

func (e *testEnv) request(method, path string, headers map[string]string, body string) (int, http.Header, []byte, error) {
	return e.requestBytes(method, path, headers, body)
}

func (e *testEnv) requestBytes(method, path string, headers map[string]string, body string) (int, http.Header, []byte, error) {
	var rdr io.Reader
	if body != "" {
		rdr = bytes.NewReader([]byte(body))
	}
	req, err := http.NewRequest(method, e.srv.URL+path, rdr)
	if err != nil {
		return 0, nil, nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := e.client.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, resp.Header, raw, nil
}

// provisionScreen creates a screen and returns the one-time raw token.
func (e *testEnv) provisionScreen(storeID int64, name string) string {
	st, data := e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/screens", storeID),
		fmt.Sprintf(`{"name":%q}`, name))
	if st != http.StatusCreated {
		e.t.Fatalf("create screen: %d %v", st, data)
	}
	return data["token"].(string)
}

// addDish creates a dish and returns nothing; fails the test on error.
func (e *testEnv) addDish(storeID int64, sku, name string, price int64, limit *int64) {
	lim := "null"
	if limit != nil {
		lim = fmt.Sprintf("%d", *limit)
	}
	body := fmt.Sprintf(`{"sku":%q,"name":%q,"base_price":%d,"active":true,"daily_limit":%s}`,
		sku, name, price, lim)
	st, data := e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/dishes", storeID), body)
	if st != http.StatusCreated {
		e.t.Fatalf("add dish %s: %d %v", sku, st, data)
	}
}

// publish posts a publish and returns status and body.
func (e *testEnv) publish(storeID int64, body string) (int, map[string]any) {
	return e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/publish", storeID), body)
}

// dishIDBySku returns a dish's id within a store.
func (e *testEnv) dishIDBySku(storeID int64, sku string) int64 {
	e.t.Helper()
	st, data := e.adminAny(http.MethodGet,
		fmt.Sprintf("/v1/admin/stores/%d/dishes", storeID), "")
	if st != http.StatusOK {
		e.t.Fatalf("list dishes: %d", st)
	}
	for _, it := range data.([]any) {
		m := it.(map[string]any)
		if m["sku"] == sku {
			return int64(m["id"].(float64))
		}
	}
	e.t.Fatalf("dish %s not found", sku)
	return 0
}
