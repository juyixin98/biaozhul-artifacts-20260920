package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/db"
	"signalboard/internal/migrate"
	"signalboard/internal/server"
)

var testPool *pgxpool.Pool

func TestMain(m *testing.M) {
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		url = "postgres://signalboard:signalboard@localhost:5432/signalboard_test?sslmode=disable"
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		fmt.Println("SKIP: cannot parse test database URL:", err)
		os.Exit(0)
	}
	if err := pool.Ping(ctx); err != nil {
		fmt.Println("SKIP: test database not reachable:", err)
		os.Exit(0)
	}
	if err := migrate.Up(ctx, pool); err != nil {
		fmt.Println("FATAL: migrations:", err)
		os.Exit(1)
	}
	testPool = pool
	os.Exit(m.Run())
}

// newAPI resets all data and returns a test server.
func newAPI(t *testing.T) *httptest.Server {
	t.Helper()
	if _, err := testPool.Exec(context.Background(), `TRUNCATE stores CASCADE`); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(server.New(db.New(testPool), testPool))
	t.Cleanup(srv.Close)
	return srv
}

type apiResp struct {
	status int
	header http.Header
	body   []byte
}

func (r apiResp) json(t *testing.T) map[string]any {
	t.Helper()
	var v map[string]any
	if err := json.Unmarshal(r.body, &v); err != nil {
		t.Fatalf("response is not JSON: %v\n%s", err, r.body)
	}
	return v
}

func doReq(t *testing.T, method, url string, body any, headers map[string]string) apiResp {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return apiResp{status: resp.StatusCode, header: resp.Header, body: b}
}

func createStore(t *testing.T, base, name, tz string) string {
	t.Helper()
	r := doReq(t, "POST", base+"/api/stores", map[string]any{"name": name, "timezone": tz}, nil)
	if r.status != http.StatusCreated {
		t.Fatalf("create store: status %d: %s", r.status, r.body)
	}
	return r.json(t)["id"].(string)
}

func upsertItem(t *testing.T, base, storeID, key string, priceCents, threshold int) {
	t.Helper()
	r := doReq(t, "PUT", base+"/api/stores/"+storeID+"/draft/items/"+key, map[string]any{
		"name": key, "price_cents": priceCents, "sold_out_threshold": threshold, "position": 1,
	}, nil)
	if r.status != http.StatusOK {
		t.Fatalf("upsert item: status %d: %s", r.status, r.body)
	}
}

func publish(t *testing.T, base, storeID string, expected int) apiResp {
	t.Helper()
	return doReq(t, "POST", base+"/api/stores/"+storeID+"/publish",
		map[string]any{"expected_version": expected}, nil)
}

func mustPublish(t *testing.T, base, storeID string, expected int) {
	t.Helper()
	if r := publish(t, base, storeID, expected); r.status != http.StatusCreated {
		t.Fatalf("publish: status %d: %s", r.status, r.body)
	}
}

func createScreen(t *testing.T, base, storeID string) string {
	t.Helper()
	r := doReq(t, "POST", base+"/api/stores/"+storeID+"/screens", map[string]any{"name": "s1"}, nil)
	if r.status != http.StatusCreated {
		t.Fatalf("create screen: status %d: %s", r.status, r.body)
	}
	return r.json(t)["token"].(string)
}

func screenMenu(t *testing.T, base, token string, headers map[string]string) apiResp {
	t.Helper()
	h := map[string]string{"Authorization": "Bearer " + token}
	for k, v := range headers {
		h[k] = v
	}
	return doReq(t, "GET", base+"/screen/menu", nil, h)
}

func menuItems(t *testing.T, r apiResp) []any {
	t.Helper()
	items, ok := r.json(t)["items"].([]any)
	if !ok {
		t.Fatalf("no items in response: %s", r.body)
	}
	return items
}

func findItem(t *testing.T, items []any, key string) map[string]any {
	t.Helper()
	for _, it := range items {
		m := it.(map[string]any)
		if m["item_key"] == key {
			return m
		}
	}
	t.Fatalf("item %q not found in %v", key, items)
	return nil
}

// --- publish concurrency & versioning --------------------------------------

func TestConcurrentPublishOnlyOneWins(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1299, 0)

	const n = 8
	statuses := make([]int, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			statuses[i] = publish(t, base, storeID, 0).status
		}(i)
	}
	wg.Wait()

	created, conflict := 0, 0
	for _, s := range statuses {
		switch s {
		case http.StatusCreated:
			created++
		case http.StatusConflict:
			conflict++
		default:
			t.Fatalf("unexpected status %d", s)
		}
	}
	if created != 1 || conflict != n-1 {
		t.Fatalf("want exactly 1 success and %d conflicts, got %d/%d", n-1, created, conflict)
	}
	r := doReq(t, "GET", base+"/api/stores/"+storeID, nil, nil)
	if v := r.json(t)["current_version"].(float64); v != 1 {
		t.Fatalf("current_version = %v, want 1", v)
	}
}

func TestPublishExpectedVersionMismatch(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1299, 0)
	if r := publish(t, base, storeID, 7); r.status != http.StatusConflict {
		t.Fatalf("status %d, want 409: %s", r.status, r.body)
	}
	mustPublish(t, base, storeID, 0)
	if r := publish(t, base, storeID, 0); r.status != http.StatusConflict {
		t.Fatalf("stale expected_version: status %d, want 409", r.status)
	}
}

func TestDraftDoesNotAffectLiveAndVersionsImmutable(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 0)
	mustPublish(t, base, storeID, 0)
	token := createScreen(t, base, storeID)

	// Draft edits after publish must not leak into the live menu.
	upsertItem(t, base, storeID, "burger", 2000, 0)
	upsertItem(t, base, storeID, "fries", 500, 0)
	r := screenMenu(t, base, token, nil)
	items := menuItems(t, r)
	if len(items) != 1 {
		t.Fatalf("live menu changed by draft edit: %s", r.body)
	}
	if p := findItem(t, items, "burger")["price_cents"].(float64); p != 1000 {
		t.Fatalf("published version mutated: price %v", p)
	}

	mustPublish(t, base, storeID, 1)
	r = screenMenu(t, base, token, nil)
	items = menuItems(t, r)
	if len(items) != 2 || findItem(t, items, "burger")["price_cents"].(float64) != 2000 {
		t.Fatalf("v2 not live: %s", r.body)
	}
	if v := r.json(t)["version"].(float64); v != 2 {
		t.Fatalf("version = %v, want 2", v)
	}
}

// --- batch import ------------------------------------------------------------

func TestImportRollbackOnError(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")

	// Batch with a temp price for an item that does not exist -> whole batch rolls back.
	payload := map[string]any{
		"items": []map[string]any{
			{"item_key": "burger", "name": "Burger", "price_cents": 1000},
			{"item_key": "fries", "name": "Fries", "price_cents": 500},
		},
		"temp_prices": []map[string]any{{
			"item_key": "ghost", "price_cents": 100,
			"starts_at": time.Now().UTC().Format(time.RFC3339),
			"ends_at":   time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
		}},
	}
	r := doReq(t, "POST", base+"/api/stores/"+storeID+"/draft/import", payload, nil)
	if r.status != http.StatusBadRequest {
		t.Fatalf("status %d, want 400: %s", r.status, r.body)
	}
	draft := doReq(t, "GET", base+"/api/stores/"+storeID+"/draft", nil, nil).json(t)
	if items := draft["items"].([]any); len(items) != 0 {
		t.Fatalf("batch not rolled back, draft has items: %v", items)
	}

	// Batch with internally overlapping temp prices -> 409, nothing persisted.
	now := time.Now().UTC()
	payload = map[string]any{
		"items": []map[string]any{{"item_key": "burger", "name": "Burger", "price_cents": 1000}},
		"temp_prices": []map[string]any{
			{"item_key": "burger", "price_cents": 900, "starts_at": now.Format(time.RFC3339), "ends_at": now.Add(2 * time.Hour).Format(time.RFC3339)},
			{"item_key": "burger", "price_cents": 800, "starts_at": now.Add(time.Hour).Format(time.RFC3339), "ends_at": now.Add(3 * time.Hour).Format(time.RFC3339)},
		},
	}
	r = doReq(t, "POST", base+"/api/stores/"+storeID+"/draft/import", payload, nil)
	if r.status != http.StatusConflict {
		t.Fatalf("status %d, want 409: %s", r.status, r.body)
	}
	draft = doReq(t, "GET", base+"/api/stores/"+storeID+"/draft", nil, nil).json(t)
	if items := draft["items"].([]any); len(items) != 0 {
		t.Fatalf("overlapping batch not rolled back: %v", items)
	}
}

func TestImportLimitAndSuccess(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")

	items := make([]map[string]any, 501)
	for i := range items {
		items[i] = map[string]any{"item_key": fmt.Sprintf("item-%d", i), "name": "x", "price_cents": 1}
	}
	r := doReq(t, "POST", base+"/api/stores/"+storeID+"/draft/import", map[string]any{"items": items}, nil)
	if r.status != http.StatusBadRequest {
		t.Fatalf("501 items: status %d, want 400", r.status)
	}

	items = items[:500]
	r = doReq(t, "POST", base+"/api/stores/"+storeID+"/draft/import", map[string]any{"items": items}, nil)
	if r.status != http.StatusOK {
		t.Fatalf("500 items: status %d: %s", r.status, r.body)
	}
	draft := doReq(t, "GET", base+"/api/stores/"+storeID+"/draft", nil, nil).json(t)
	if got := len(draft["items"].([]any)); got != 500 {
		t.Fatalf("imported %d items, want 500", got)
	}
}

// --- temp prices -------------------------------------------------------------

func addTempPrice(t *testing.T, base, storeID string, body map[string]any) apiResp {
	t.Helper()
	return doReq(t, "POST", base+"/api/stores/"+storeID+"/draft/temp-prices", body, nil)
}

func TestTempPriceHalfOpenAndOverlap(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 0)
	upsertItem(t, base, storeID, "fries", 500, 0)
	day := time.Now().UTC().Truncate(24 * time.Hour)
	at := func(h int) string { return day.Add(time.Duration(h) * time.Hour).Format(time.RFC3339) }

	// [10,12) ok; adjacent [12,14) ok (half-open: touching is not overlapping).
	if r := addTempPrice(t, base, storeID, map[string]any{"item_key": "burger", "price_cents": 900, "starts_at": at(10), "ends_at": at(12)}); r.status != http.StatusCreated {
		t.Fatalf("first band: %d: %s", r.status, r.body)
	}
	if r := addTempPrice(t, base, storeID, map[string]any{"item_key": "burger", "price_cents": 800, "starts_at": at(12), "ends_at": at(14)}); r.status != http.StatusCreated {
		t.Fatalf("adjacent band must be allowed: %d: %s", r.status, r.body)
	}
	// Overlapping [11,13) rejected; same window for another item allowed.
	if r := addTempPrice(t, base, storeID, map[string]any{"item_key": "burger", "price_cents": 700, "starts_at": at(11), "ends_at": at(13)}); r.status != http.StatusConflict {
		t.Fatalf("overlap: status %d, want 409: %s", r.status, r.body)
	}
	if r := addTempPrice(t, base, storeID, map[string]any{"item_key": "fries", "price_cents": 400, "starts_at": at(11), "ends_at": at(13)}); r.status != http.StatusCreated {
		t.Fatalf("other item same window: %d: %s", r.status, r.body)
	}
	// starts >= ends rejected.
	if r := addTempPrice(t, base, storeID, map[string]any{"item_key": "burger", "price_cents": 700, "starts_at": at(15), "ends_at": at(15)}); r.status != http.StatusBadRequest {
		t.Fatalf("empty band: status %d, want 400", r.status)
	}
}

func TestTempPriceConcurrentOverlap(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 0)
	now := time.Now().UTC()
	body := map[string]any{
		"item_key": "burger", "price_cents": 900,
		"starts_at": now.Format(time.RFC3339), "ends_at": now.Add(2 * time.Hour).Format(time.RFC3339),
	}
	const n = 8
	statuses := make([]int, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			statuses[i] = addTempPrice(t, base, storeID, body).status
		}(i)
	}
	wg.Wait()
	created := 0
	for _, s := range statuses {
		if s == http.StatusCreated {
			created++
		} else if s != http.StatusConflict {
			t.Fatalf("unexpected status %d", s)
		}
	}
	if created != 1 {
		t.Fatalf("concurrent overlapping bands: %d succeeded, want exactly 1", created)
	}
}

func TestTempPriceEffectiveWindow(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "active", 1000, 0)
	upsertItem(t, base, storeID, "past", 1000, 0)
	upsertItem(t, base, storeID, "future", 1000, 0)
	now := time.Now().UTC()
	band := func(key string, start, end time.Time, price int) {
		t.Helper()
		r := addTempPrice(t, base, storeID, map[string]any{
			"item_key": key, "price_cents": price,
			"starts_at": start.Format(time.RFC3339), "ends_at": end.Format(time.RFC3339),
		})
		if r.status != http.StatusCreated {
			t.Fatalf("band %s: %d: %s", key, r.status, r.body)
		}
	}
	band("active", now.Add(-time.Hour), now.Add(time.Hour), 500)
	band("past", now.Add(-2*time.Hour), now.Add(-time.Hour), 500)
	band("future", now.Add(time.Hour), now.Add(2*time.Hour), 500)
	mustPublish(t, base, storeID, 0)
	token := createScreen(t, base, storeID)

	items := menuItems(t, screenMenu(t, base, token, nil))
	if it := findItem(t, items, "active"); it["price_cents"].(float64) != 500 || it["temp_price_active"] != true {
		t.Fatalf("active band not applied: %v", it)
	}
	for _, key := range []string{"past", "future"} {
		if it := findItem(t, items, key); it["price_cents"].(float64) != 1000 || it["temp_price_active"] != false {
			t.Fatalf("%s band should be inactive: %v", key, it)
		}
	}
}

// --- sales -------------------------------------------------------------------

func postSale(t *testing.T, base, storeID, id, item string, qty int, occurred time.Time) apiResp {
	t.Helper()
	return doReq(t, "POST", base+"/api/stores/"+storeID+"/sales-events", map[string]any{
		"id": id, "item_key": item, "quantity": qty, "occurred_at": occurred.Format(time.RFC3339),
	}, nil)
}

func dailyTotal(t *testing.T, base, storeID, day, item string) float64 {
	t.Helper()
	r := doReq(t, "GET", base+"/api/stores/"+storeID+"/sales?day="+day, nil, nil)
	if r.status != http.StatusOK {
		t.Fatalf("list sales: %d: %s", r.status, r.body)
	}
	for _, row := range r.json(t)["sales"].([]any) {
		m := row.(map[string]any)
		if m["item_key"] == item {
			return m["quantity"].(float64)
		}
	}
	return 0
}

func TestSalesIdempotencyAndConflict(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 0)
	mustPublish(t, base, storeID, 0)
	now := time.Now().UTC()
	id := "11111111-1111-1111-1111-111111111111"
	today := now.Format("2006-01-02")

	if r := postSale(t, base, storeID, id, "burger", 3, now); r.status != http.StatusCreated {
		t.Fatalf("first: %d: %s", r.status, r.body)
	}
	if r := postSale(t, base, storeID, id, "burger", 3, now); r.status != http.StatusOK {
		t.Fatalf("identical retry must be 200 duplicate, got %d: %s", r.status, r.body)
	}
	if got := dailyTotal(t, base, storeID, today, "burger"); got != 3 {
		t.Fatalf("duplicate counted twice: total %v", got)
	}
	if r := postSale(t, base, storeID, id, "burger", 5, now); r.status != http.StatusConflict {
		t.Fatalf("same id different content must be 409, got %d", r.status)
	}
}

func TestSalesConcurrentDuplicatesCountedOnce(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 0)
	mustPublish(t, base, storeID, 0)
	now := time.Now().UTC()
	id := "22222222-2222-2222-2222-222222222222"

	const n = 10
	statuses := make([]int, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			statuses[i] = postSale(t, base, storeID, id, "burger", 2, now).status
		}(i)
	}
	wg.Wait()
	created := 0
	for _, s := range statuses {
		if s == http.StatusCreated {
			created++
		} else if s != http.StatusOK {
			t.Fatalf("unexpected status %d", s)
		}
	}
	if created != 1 {
		t.Fatalf("%d concurrent inserts succeeded, want 1", created)
	}
	if got := dailyTotal(t, base, storeID, now.Format("2006-01-02"), "burger"); got != 2 {
		t.Fatalf("total %v, want 2 (counted exactly once)", got)
	}
}

func TestSalesLateEventAndCrossDayRecovery(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 2) // threshold 2
	mustPublish(t, base, storeID, 0)
	token := createScreen(t, base, storeID)

	loc, _ := time.LoadLocation("Asia/Shanghai")
	yesterday := time.Now().In(loc).AddDate(0, 0, -1)
	// Late event: happened yesterday noon store-local, arrives now.
	occurred := time.Date(yesterday.Year(), yesterday.Month(), yesterday.Day(), 12, 0, 0, 0, loc)
	if r := postSale(t, base, storeID, "33333333-3333-3333-3333-333333333333", "burger", 5, occurred); r.status != http.StatusCreated {
		t.Fatalf("late event: %d: %s", r.status, r.body)
	}
	// Attributed to the actual day, not the arrival day.
	if got := dailyTotal(t, base, storeID, occurred.Format("2006-01-02"), "burger"); got != 5 {
		t.Fatalf("late event not attributed to occurred day: %v", got)
	}
	if got := dailyTotal(t, base, storeID, time.Now().In(loc).Format("2006-01-02"), "burger"); got != 0 {
		t.Fatalf("late event leaked into today: %v", got)
	}
	// Threshold exceeded yesterday, but today is a new day: not sold out.
	items := menuItems(t, screenMenu(t, base, token, nil))
	if it := findItem(t, items, "burger"); it["sold_out"] != false {
		t.Fatalf("sold_out did not recover across day boundary: %v", it)
	}
	// Reach today's threshold -> sold out now.
	if r := postSale(t, base, storeID, "44444444-4444-4444-4444-444444444444", "burger", 2, time.Now()); r.status != http.StatusCreated {
		t.Fatalf("today event: %d: %s", r.status, r.body)
	}
	items = menuItems(t, screenMenu(t, base, token, nil))
	if it := findItem(t, items, "burger"); it["sold_out"] != true {
		t.Fatalf("threshold reached but not sold out: %v", it)
	}
}

// --- screens: auth, ETag, heartbeat ------------------------------------------

func TestScreenAuthAndIsolation(t *testing.T) {
	base := newAPI(t).URL
	storeA := createStore(t, base, "A", "Asia/Shanghai")
	storeB := createStore(t, base, "B", "Asia/Shanghai")
	upsertItem(t, base, storeA, "only-a", 100, 0)
	upsertItem(t, base, storeB, "only-b", 100, 0)
	mustPublish(t, base, storeA, 0)
	mustPublish(t, base, storeB, 0)
	tokenA := createScreen(t, base, storeA)

	if r := doReq(t, "GET", base+"/screen/menu", nil, nil); r.status != http.StatusUnauthorized {
		t.Fatalf("no token: %d, want 401", r.status)
	}
	if r := screenMenu(t, base, "sb_wrong", nil); r.status != http.StatusUnauthorized {
		t.Fatalf("bad token: %d, want 401", r.status)
	}
	items := menuItems(t, screenMenu(t, base, tokenA, nil))
	if len(items) != 1 || items[0].(map[string]any)["item_key"] != "only-a" {
		t.Fatalf("screen saw another store's menu: %v", items)
	}
	if r := doReq(t, "POST", base+"/screen/heartbeat", nil, nil); r.status != http.StatusUnauthorized {
		t.Fatalf("heartbeat without token: %d, want 401", r.status)
	}
}

func TestETagChangesOnStateChange(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	upsertItem(t, base, storeID, "burger", 1000, 1)
	mustPublish(t, base, storeID, 0)
	token := createScreen(t, base, storeID)

	r1 := screenMenu(t, base, token, nil)
	if r1.status != http.StatusOK {
		t.Fatalf("menu: %d", r1.status)
	}
	etag1 := r1.header.Get("ETag")
	if etag1 == "" {
		t.Fatal("missing ETag")
	}
	// Conditional request with the fresh ETag -> 304, no body.
	r2 := screenMenu(t, base, token, map[string]string{"If-None-Match": etag1})
	if r2.status != http.StatusNotModified || len(r2.body) != 0 {
		t.Fatalf("conditional GET: status %d body %q, want 304 empty", r2.status, r2.body)
	}
	// Sold-out flip changes the ETag.
	postSale(t, base, storeID, "55555555-5555-5555-5555-555555555555", "burger", 1, time.Now())
	r3 := screenMenu(t, base, token, map[string]string{"If-None-Match": etag1})
	if r3.status != http.StatusOK || r3.header.Get("ETag") == etag1 {
		t.Fatalf("sold-out change must invalidate cache: status %d etag %q", r3.status, r3.header.Get("ETag"))
	}
	// Publish (price change) changes the ETag again.
	upsertItem(t, base, storeID, "burger", 800, 1)
	mustPublish(t, base, storeID, 1)
	r4 := screenMenu(t, base, token, map[string]string{"If-None-Match": r3.header.Get("ETag")})
	if r4.status != http.StatusOK || r4.header.Get("ETag") == r3.header.Get("ETag") {
		t.Fatalf("publish must invalidate cache: status %d", r4.status)
	}
	if p := findItem(t, menuItems(t, r4), "burger")["price_cents"].(float64); p != 800 {
		t.Fatalf("new price not live: %v", p)
	}
	// Stale ETag never matches again.
	if r := screenMenu(t, base, token, map[string]string{"If-None-Match": etag1}); r.status != http.StatusOK {
		t.Fatalf("stale etag must not hit cache: %d", r.status)
	}
}

func TestHeartbeatOnlineOffline(t *testing.T) {
	base := newAPI(t).URL
	storeID := createStore(t, base, "A", "Asia/Shanghai")
	token := createScreen(t, base, storeID)

	online := func() bool {
		r := doReq(t, "GET", base+"/api/stores/"+storeID+"/screens", nil, nil)
		return r.json(t)["screens"].([]any)[0].(map[string]any)["online"].(bool)
	}
	if online() {
		t.Fatal("screen without heartbeat must be offline")
	}
	r := doReq(t, "POST", base+"/screen/heartbeat", nil, map[string]string{"Authorization": "Bearer " + token})
	if r.status != http.StatusNoContent {
		t.Fatalf("heartbeat: %d: %s", r.status, r.body)
	}
	if !online() {
		t.Fatal("screen must be online after heartbeat")
	}
	// Simulate a heartbeat older than 90 seconds -> offline again.
	if _, err := testPool.Exec(context.Background(),
		`UPDATE screens SET last_heartbeat_at = now() - interval '120 seconds'`); err != nil {
		t.Fatal(err)
	}
	if online() {
		t.Fatal("screen with 120s-old heartbeat must be offline")
	}
}
