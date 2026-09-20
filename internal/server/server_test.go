package server

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

	"signalboard/internal/migrate"
)

const adminToken = "test-admin-token"

// --- test harness ---------------------------------------------------------------

// fakeClock follows real time until set() freezes it at a fixed instant.
type fakeClock struct {
	mu    sync.Mutex
	fixed *time.Time
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fixed != nil {
		return *c.fixed
	}
	return time.Now()
}

func (c *fakeClock) set(t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.fixed = &t
}

type env struct {
	t    *testing.T
	srv  *Server
	http *httptest.Server
	clk  *fakeClock
}

func setup(t *testing.T) *env {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping integration test")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Skipf("cannot connect to test database: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		t.Skipf("cannot reach test database: %v", err)
	}
	if _, err := pool.Exec(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset schema: %v", err)
	}
	if err := migrate.Up(ctx, pool); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	srv := New(pool, adminToken)
	clk := &fakeClock{}
	srv.now = clk.now
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(func() { ts.Close(); pool.Close() })
	return &env{t: t, srv: srv, http: ts, clk: clk}
}

func (e *env) do(method, path, token string, body any) (int, http.Header, []byte) {
	e.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			e.t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, e.http.URL+path, rdr)
	if err != nil {
		e.t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		e.t.Fatal(err)
	}
	return resp.StatusCode, resp.Header, data
}

func decode(t *testing.T, data []byte, v any) {
	t.Helper()
	if err := json.Unmarshal(data, v); err != nil {
		t.Fatalf("decode %s: %v", data, err)
	}
}

// --- convenience ------------------------------------------------------------------

func (e *env) createStore(tz string) string {
	e.t.Helper()
	status, _, body := e.do("POST", "/v1/stores", adminToken, map[string]any{"name": "Store", "timezone": tz})
	if status != http.StatusCreated {
		e.t.Fatalf("create store: %d %s", status, body)
	}
	var resp storeResp
	decode(e.t, body, &resp)
	return resp.ID.String()
}

func (e *env) createItem(storeID, name string, price, threshold int32) string {
	e.t.Helper()
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/items", adminToken, map[string]any{
		"name": name, "price_cents": price, "sold_out_threshold": threshold,
	})
	if status != http.StatusCreated {
		e.t.Fatalf("create item: %d %s", status, body)
	}
	var resp itemResp
	decode(e.t, body, &resp)
	return resp.ID.String()
}

func (e *env) publish(storeID string, expected int32) (int, []byte) {
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/publish", adminToken,
		map[string]any{"expected_version": expected})
	return status, body
}

func (e *env) mustPublish(storeID string, expected int32) {
	e.t.Helper()
	status, body := e.publish(storeID, expected)
	if status != http.StatusCreated {
		e.t.Fatalf("publish: %d %s", status, body)
	}
}

func (e *env) createScreen(storeID string) string {
	e.t.Helper()
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/screens", adminToken, map[string]any{"name": "Screen 1"})
	if status != http.StatusCreated {
		e.t.Fatalf("create screen: %d %s", status, body)
	}
	var resp struct {
		Token string `json:"token"`
	}
	decode(e.t, body, &resp)
	return resp.Token
}

type menuResponse struct {
	Version int32 `json:"version"`
	Items   []struct {
		ItemID         string `json:"item_id"`
		PriceCents     int32  `json:"price_cents"`
		BasePriceCents int32  `json:"base_price_cents"`
		SoldOut        bool   `json:"sold_out"`
	} `json:"items"`
}

func (e *env) getMenu(screenToken, ifNoneMatch string) (int, string, menuResponse) {
	e.t.Helper()
	req, err := http.NewRequest("GET", e.http.URL+"/v1/screen/menu", nil)
	if err != nil {
		e.t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+screenToken)
	if ifNoneMatch != "" {
		req.Header.Set("If-None-Match", ifNoneMatch)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var menu menuResponse
	if resp.StatusCode == http.StatusOK {
		decode(e.t, data, &menu)
	}
	return resp.StatusCode, resp.Header.Get("ETag"), menu
}

// --- tests ------------------------------------------------------------------------

func TestConcurrentPublish(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("Asia/Shanghai")
	e.createItem(storeID, "Burger", 1200, 0)

	const n = 8
	var wg sync.WaitGroup
	statuses := make([]int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			status, _ := e.publish(storeID, 0)
			statuses[i] = status
		}(i)
	}
	wg.Wait()

	created, conflicts := 0, 0
	for _, s := range statuses {
		switch s {
		case http.StatusCreated:
			created++
		case http.StatusConflict:
			conflicts++
		default:
			t.Fatalf("unexpected status %d", s)
		}
	}
	if created != 1 || conflicts != n-1 {
		t.Fatalf("want 1 created + %d conflicts, got %d + %d", n-1, created, conflicts)
	}

	// Exactly one version exists.
	_, _, body := e.do("GET", "/v1/stores/"+storeID, adminToken, nil)
	var st storeResp
	decode(t, body, &st)
	if st.CurrentVersion != 1 {
		t.Fatalf("current_version = %d, want 1", st.CurrentVersion)
	}
}

func TestPublishStaleExpectedVersion(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")
	e.createItem(storeID, "Fries", 300, 0)
	e.mustPublish(storeID, 0)
	if status, _ := e.publish(storeID, 0); status != http.StatusConflict {
		t.Fatalf("stale expected_version: got %d, want 409", status)
	}
	e.mustPublish(storeID, 1)
}

func TestBatchImportRollback(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")

	items := make([]map[string]any, 10)
	for i := range items {
		items[i] = map[string]any{"name": fmt.Sprintf("item-%d", i), "price_cents": 100}
	}
	items[4]["price_cents"] = -5 // invalid -> whole batch must roll back
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/items/batch-import", adminToken,
		map[string]any{"items": items})
	if status != http.StatusBadRequest {
		t.Fatalf("invalid batch: got %d, want 400 (%s)", status, body)
	}
	_, _, body = e.do("GET", "/v1/stores/"+storeID+"/items", adminToken, nil)
	var got []itemResp
	decode(t, body, &got)
	if len(got) != 0 {
		t.Fatalf("batch was not rolled back: %d items present", len(got))
	}

	// Over the 500-item limit.
	big := make([]map[string]any, MaxBatchImportItems+1)
	for i := range big {
		big[i] = map[string]any{"name": fmt.Sprintf("x-%d", i), "price_cents": 1}
	}
	status, _, _ = e.do("POST", "/v1/stores/"+storeID+"/items/batch-import", adminToken,
		map[string]any{"items": big})
	if status != http.StatusBadRequest {
		t.Fatalf("oversized batch: got %d, want 400", status)
	}

	// A valid batch of exactly 500 succeeds atomically.
	big = big[:MaxBatchImportItems]
	status, _, body = e.do("POST", "/v1/stores/"+storeID+"/items/batch-import", adminToken,
		map[string]any{"items": big})
	if status != http.StatusCreated {
		t.Fatalf("500-item batch: got %d (%s)", status, body)
	}
	_, _, body = e.do("GET", "/v1/stores/"+storeID+"/items", adminToken, nil)
	decode(t, body, &got)
	if len(got) != MaxBatchImportItems {
		t.Fatalf("imported %d items, want %d", len(got), MaxBatchImportItems)
	}
}

func TestDraftDoesNotAffectLiveAndVersionsImmutable(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")
	itemID := e.createItem(storeID, "Cola", 250, 0)
	e.mustPublish(storeID, 0)
	screen := e.createScreen(storeID)

	// Draft edit after publish must not leak into the live menu.
	status, _, _ := e.do("PUT", "/v1/stores/"+storeID+"/items/"+itemID, adminToken,
		map[string]any{"name": "Cola", "price_cents": 999})
	if status != http.StatusOK {
		t.Fatalf("update item: %d", status)
	}
	_, _, menu := e.getMenu(screen, "")
	if menu.Items[0].PriceCents != 250 {
		t.Fatalf("draft edit leaked to live menu: price %d", menu.Items[0].PriceCents)
	}

	e.mustPublish(storeID, 1)
	_, _, menu = e.getMenu(screen, "")
	if menu.Items[0].PriceCents != 999 {
		t.Fatalf("republish did not update live menu: price %d", menu.Items[0].PriceCents)
	}

	// Version 1 remains immutable with the original price.
	_, _, body := e.do("GET", "/v1/stores/"+storeID+"/versions/1", adminToken, nil)
	var v struct {
		Items []struct {
			PriceCents int32 `json:"price_cents"`
		} `json:"items"`
	}
	decode(t, body, &v)
	if v.Items[0].PriceCents != 250 {
		t.Fatalf("version 1 mutated: price %d", v.Items[0].PriceCents)
	}
}

func TestTempPriceBoundaries(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")
	itemID := e.createItem(storeID, "Latte", 500, 0)
	e.mustPublish(storeID, 0)
	screen := e.createScreen(storeID)

	// Window with a UTC offset in the input: 18:00-19:00 at +08:00 == 10:00-11:00 UTC.
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/temp-prices", adminToken, map[string]any{
		"item_id": itemID, "price_cents": 399,
		"starts_at": "2026-09-20T18:00:00+08:00", "ends_at": "2026-09-20T19:00:00+08:00",
	})
	if status != http.StatusCreated {
		t.Fatalf("create temp price: %d %s", status, body)
	}

	priceAt := func(ts time.Time) int32 {
		e.clk.set(ts)
		_, _, menu := e.getMenu(screen, "")
		return menu.Items[0].PriceCents
	}
	utc := time.UTC
	if p := priceAt(time.Date(2026, 9, 20, 9, 59, 59, 0, utc)); p != 500 {
		t.Fatalf("before window: price %d, want 500", p)
	}
	if p := priceAt(time.Date(2026, 9, 20, 10, 0, 0, 0, utc)); p != 399 {
		t.Fatalf("at window start (inclusive): price %d, want 399", p)
	}
	if p := priceAt(time.Date(2026, 9, 20, 10, 59, 59, 0, utc)); p != 399 {
		t.Fatalf("inside window: price %d, want 399", p)
	}
	if p := priceAt(time.Date(2026, 9, 20, 11, 0, 0, 0, utc)); p != 500 {
		t.Fatalf("at window end (exclusive): price %d, want 500", p)
	}

	// Overlapping window rejected; adjacent (touching) window allowed.
	status, _, _ = e.do("POST", "/v1/stores/"+storeID+"/temp-prices", adminToken, map[string]any{
		"item_id": itemID, "price_cents": 299,
		"starts_at": "2026-09-20T10:30:00Z", "ends_at": "2026-09-20T12:00:00Z",
	})
	if status != http.StatusConflict {
		t.Fatalf("overlapping window: got %d, want 409", status)
	}
	status, _, body = e.do("POST", "/v1/stores/"+storeID+"/temp-prices", adminToken, map[string]any{
		"item_id": itemID, "price_cents": 299,
		"starts_at": "2026-09-20T11:00:00Z", "ends_at": "2026-09-20T12:00:00Z",
	})
	if status != http.StatusCreated {
		t.Fatalf("adjacent window: got %d (%s), want 201", status, body)
	}
	// Inverted interval rejected.
	status, _, _ = e.do("POST", "/v1/stores/"+storeID+"/temp-prices", adminToken, map[string]any{
		"item_id": itemID, "price_cents": 299,
		"starts_at": "2026-09-21T12:00:00Z", "ends_at": "2026-09-21T11:00:00Z",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("inverted window: got %d, want 400", status)
	}
}

func TestTempPriceConcurrentOverlap(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")
	itemID := e.createItem(storeID, "Tea", 200, 0)

	const n = 10
	var wg sync.WaitGroup
	statuses := make([]int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			status, _, _ := e.do("POST", "/v1/stores/"+storeID+"/temp-prices", adminToken, map[string]any{
				"item_id": itemID, "price_cents": 150,
				"starts_at": "2026-10-01T10:00:00Z", "ends_at": "2026-10-01T11:00:00Z",
			})
			statuses[i] = status
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
		t.Fatalf("concurrent overlapping windows: %d created, want exactly 1", created)
	}
}

func TestSalesIdempotency(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("Asia/Shanghai")
	itemID := e.createItem(storeID, "Rice", 800, 0)
	e.mustPublish(storeID, 0)

	ev := map[string]any{
		"event_id": "evt-1", "item_id": itemID, "quantity": 2,
		"occurred_at": "2026-09-20T01:00:00Z",
	}
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, ev)
	if status != http.StatusCreated {
		t.Fatalf("first event: %d %s", status, body)
	}
	var first salesEventResp
	decode(t, body, &first)
	if first.DailyQuantity != 2 || first.Duplicate {
		t.Fatalf("first event: %+v", first)
	}

	// Identical replay: counted once.
	status, _, body = e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, ev)
	if status != http.StatusOK {
		t.Fatalf("replay: got %d, want 200 (%s)", status, body)
	}
	var dup salesEventResp
	decode(t, body, &dup)
	if !dup.Duplicate || dup.DailyQuantity != 2 {
		t.Fatalf("replay changed state: %+v", dup)
	}

	// Same id, different payload: conflict.
	ev["quantity"] = 5
	status, _, _ = e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, ev)
	if status != http.StatusConflict {
		t.Fatalf("conflicting payload: got %d, want 409", status)
	}

	// Concurrent duplicates: exactly one counts.
	const n = 16
	var wg sync.WaitGroup
	qtys := make([]int64, n)
	ev["quantity"] = 3
	ev["event_id"] = "evt-2"
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _, b := e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, ev)
			var r salesEventResp
			if err := json.Unmarshal(b, &r); err == nil {
				qtys[i] = r.DailyQuantity
			}
		}(i)
	}
	wg.Wait()
	_, _, body = e.do("GET", "/v1/stores/"+storeID+"/sales/daily?date=2026-09-20", adminToken, nil)
	var daily struct {
		Items []struct {
			ItemID   string `json:"item_id"`
			Quantity int64  `json:"quantity"`
		} `json:"items"`
	}
	decode(t, body, &daily)
	if len(daily.Items) != 1 || daily.Items[0].Quantity != 5 { // 2 + 3, not 2 + 16*3
		t.Fatalf("concurrent duplicates miscounted: %+v", daily.Items)
	}
}

func TestSalesThresholdSoldOutAndCrossDayRestore(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")
	itemID := e.createItem(storeID, "Soup", 600, 3) // threshold 3/day
	e.mustPublish(storeID, 0)
	screen := e.createScreen(storeID)

	e.clk.set(time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC))
	post := func(id string, qty int32) {
		t.Helper()
		status, _, body := e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, map[string]any{
			"event_id": id, "item_id": itemID, "quantity": qty,
			"occurred_at": "2026-09-20T11:00:00Z",
		})
		if status != http.StatusCreated {
			t.Fatalf("sales event %s: %d %s", id, status, body)
		}
	}
	post("s-1", 2)
	_, _, menu := e.getMenu(screen, "")
	if menu.Items[0].SoldOut {
		t.Fatal("sold out below threshold")
	}
	post("s-2", 1) // reaches threshold 3
	_, _, menu = e.getMenu(screen, "")
	if !menu.Items[0].SoldOut {
		t.Fatal("not sold out at threshold")
	}

	// Next day: automatically available again, no manual reset.
	e.clk.set(time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC))
	_, _, menu = e.getMenu(screen, "")
	if menu.Items[0].SoldOut {
		t.Fatal("sold out state leaked across days")
	}
}

func TestLateEventAttributedToOccurredDate(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("Asia/Shanghai")
	itemID := e.createItem(storeID, "Noodles", 900, 0)
	e.mustPublish(storeID, 0)

	// Sent "now" but occurred yesterday late night UTC = today early morning +08:00.
	e.clk.set(time.Date(2026, 9, 21, 8, 0, 0, 0, time.UTC))
	status, _, body := e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, map[string]any{
		"event_id": "late-1", "item_id": itemID, "quantity": 4,
		"occurred_at": "2026-09-19T20:30:00Z", // 2026-09-20 04:30 in Asia/Shanghai
	})
	if status != http.StatusCreated {
		t.Fatalf("late event: %d %s", status, body)
	}
	var resp salesEventResp
	decode(t, body, &resp)
	if resp.SaleDate != "2026-09-20" {
		t.Fatalf("sale_date = %s, want 2026-09-20 (store-local occurred date)", resp.SaleDate)
	}
	_, _, body = e.do("GET", "/v1/stores/"+storeID+"/sales/daily?date=2026-09-20", adminToken, nil)
	var daily struct {
		Items []struct {
			Quantity int64 `json:"quantity"`
		} `json:"items"`
	}
	decode(t, body, &daily)
	if len(daily.Items) != 1 || daily.Items[0].Quantity != 4 {
		t.Fatalf("late event not attributed to occurred date: %s", body)
	}
}

func TestETagInvalidation(t *testing.T) {
	e := setup(t)
	storeID := e.createStore("UTC")
	itemID := e.createItem(storeID, "Cake", 700, 2)
	e.mustPublish(storeID, 0)
	screen := e.createScreen(storeID)

	status, etag1, _ := e.getMenu(screen, "")
	if status != http.StatusOK || etag1 == "" {
		t.Fatalf("initial menu: %d etag=%q", status, etag1)
	}
	// Conditional request with the current ETag -> 304.
	status, _, _ = e.getMenu(screen, etag1)
	if status != http.StatusNotModified {
		t.Fatalf("If-None-Match: got %d, want 304", status)
	}

	// Publish changes the ETag; the old one must not hit the cache.
	e.createItem(storeID, "Pie", 450, 0)
	e.mustPublish(storeID, 1)
	status, etag2, _ := e.getMenu(screen, etag1)
	if status != http.StatusOK || etag2 == etag1 {
		t.Fatalf("after publish: status %d, etag unchanged", status)
	}

	// Sold-out flip changes the ETag.
	e.do("POST", "/v1/stores/"+storeID+"/sales-events", adminToken, map[string]any{
		"event_id": "e-1", "item_id": itemID, "quantity": 2, "occurred_at": time.Now().UTC(),
	})
	status, etag3, menu := e.getMenu(screen, etag2)
	soldOut := false
	for _, it := range menu.Items {
		if it.ItemID == itemID {
			soldOut = it.SoldOut
		}
	}
	if status != http.StatusOK || etag3 == etag2 || !soldOut {
		t.Fatalf("after sold-out: status %d etag same=%v soldout=%v", status, etag3 == etag2, soldOut)
	}

	// Temp-price activation (time-based, no republish) changes the ETag.
	e.clk.set(time.Date(2026, 9, 20, 9, 0, 0, 0, time.UTC))
	e.do("POST", "/v1/stores/"+storeID+"/temp-prices", adminToken, map[string]any{
		"item_id": itemID, "price_cents": 555,
		"starts_at": "2026-09-20T10:00:00Z", "ends_at": "2026-09-20T11:00:00Z",
	})
	_, etag4, _ := e.getMenu(screen, "")
	e.clk.set(time.Date(2026, 9, 20, 10, 30, 0, 0, time.UTC))
	status, etag5, menu := e.getMenu(screen, etag4)
	var cakePrice int32
	for _, it := range menu.Items {
		if it.ItemID == itemID {
			cakePrice = it.PriceCents
		}
	}
	if status != http.StatusOK || etag5 == etag4 || cakePrice != 555 {
		t.Fatalf("temp price activation did not bust cache: status %d price %d", status, cakePrice)
	}
}

func TestScreenAuthAndStoreIsolation(t *testing.T) {
	e := setup(t)
	storeA := e.createStore("UTC")
	storeB := e.createStore("UTC")
	e.createItem(storeA, "A-Meal", 100, 0)
	e.createItem(storeB, "B-Meal", 200, 0)
	e.mustPublish(storeA, 0)
	e.mustPublish(storeB, 0)
	screenA := e.createScreen(storeA)

	// No token / garbage token.
	req, _ := http.NewRequest("GET", e.http.URL+"/v1/screen/menu", nil)
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no token: %d, want 401", resp.StatusCode)
	}
	status, _, _ := e.getMenu("sb_scr_invalid", "")
	if status != http.StatusUnauthorized {
		t.Fatalf("bad token: %d, want 401", status)
	}

	// Screen A sees only store A's menu.
	_, _, menu := e.getMenu(screenA, "")
	if len(menu.Items) != 1 || menu.Items[0].PriceCents != 100 {
		t.Fatalf("screen saw wrong store menu: %+v", menu.Items)
	}

	// Admin endpoints require the admin token.
	status, _, _ = e.do("GET", "/v1/stores", screenA, nil)
	if status != http.StatusUnauthorized {
		t.Fatalf("screen token on admin route: %d, want 401", status)
	}
}

func TestHeartbeatOfflineDetection(t *testing.T) {
	e := setup(t)
	e.srv.offlineAfter = 100 * time.Millisecond
	storeID := e.createStore("UTC")
	screen := e.createScreen(storeID)

	online := func() bool {
		t.Helper()
		_, _, body := e.do("GET", "/v1/stores/"+storeID+"/screens", adminToken, nil)
		var screens []screenResp
		decode(t, body, &screens)
		return screens[0].Online
	}
	if online() {
		t.Fatal("screen online before any heartbeat")
	}
	status, _, _ := e.do("POST", "/v1/screen/heartbeat", screen, nil)
	if status != http.StatusOK {
		t.Fatalf("heartbeat: %d", status)
	}
	if !online() {
		t.Fatal("screen offline right after heartbeat")
	}
	time.Sleep(200 * time.Millisecond)
	if online() {
		t.Fatal("screen still online past the heartbeat timeout")
	}
}

func TestMatchETag(t *testing.T) {
	if !matchETag(`"abc"`, `"abc"`) {
		t.Fatal("exact match failed")
	}
	if !matchETag(`"x", "abc"`, `"abc"`) {
		t.Fatal("list match failed")
	}
	if !matchETag(`*`, `"abc"`) {
		t.Fatal("wildcard match failed")
	}
	if matchETag(`"other"`, `"abc"`) {
		t.Fatal("mismatch accepted")
	}
	if matchETag("", `"abc"`) {
		t.Fatal("empty header accepted")
	}
}
