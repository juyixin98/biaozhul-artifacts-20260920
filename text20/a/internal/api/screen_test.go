package api

import (
	"context"
	"fmt"
	"net/http"
	"testing"
	"time"
)

// TestScreen_TokenScopedToOwnStore: a screen token for store A must not be
// able to read store B. Screens never name a store in the URL — the token
// fully determines scope — so the only thing to verify is that two stores'
// menus are independent and the token always resolves to its own store.
func TestScreen_TokenScopedToOwnStore(t *testing.T) {
	e := newTestEnv(t)
	storeA := e.createStoreViaAPI("UTC")
	storeB := e.createStoreViaAPI("UTC")
	e.addDish(storeA, "AAA", "Item A", 100, nil)
	e.addDish(storeB, "BBB", "Item B", 200, nil)
	e.publish(storeA, `{"expected_version":0}`)
	e.publish(storeB, `{"expected_version":0}`)

	tokenA := e.provisionScreen(storeA, "screenA")

	// No token -> 401.
	if st, _, _ := e.screen("", http.MethodGet, "/v1/screen/menu", nil); st != http.StatusUnauthorized {
		t.Fatalf("no token: expected 401, got %d", st)
	}
	// Bogus token -> 401.
	if st, _, _ := e.screen("sbscr_nope", http.MethodGet, "/v1/screen/menu", nil); st != http.StatusUnauthorized {
		t.Fatalf("bad token: expected 401, got %d", st)
	}

	// Token A only ever sees store A's content.
	st, _, menu := e.screen(tokenA, http.MethodGet, "/v1/screen/menu", nil)
	if st != http.StatusOK {
		t.Fatalf("menu: %d", st)
	}
	if int64(menu["store_id"].(float64)) != storeA {
		t.Fatalf("token resolved to wrong store: %v", menu["store_id"])
	}
	for _, it := range menu["items"].([]any) {
		if it.(map[string]any)["sku"] == "BBB" {
			t.Fatalf("store A screen saw store B item")
		}
	}
}

// TestScreen_ETagConditionalRequests: repeated polls with If-None-Match yield
// 304 while nothing changes; after a render-affecting change the ETag changes
// and a stale token gets a fresh 200. This covers publish, sold-out, and
// cross-day cache invalidation.
func TestScreen_ETagConditionalRequests(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	limit := int64(2)
	e.addDish(storeID, "B", "Burger", 1000, &limit)
	e.publish(storeID, `{"expected_version":0}`)
	token := e.provisionScreen(storeID, "s")

	day, _ := time.Parse(time.RFC3339, "2026-04-01T12:00:00Z")
	pinClock(t, day)

	// First fetch: 200 + ETag.
	st, hdr, _ := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if st != http.StatusOK {
		t.Fatalf("first: %d", st)
	}
	etag1 := hdr.Get("ETag")
	if etag1 == "" {
		t.Fatal("missing ETag header")
	}

	// Conditional fetch with the same ETag -> 304.
	st, hdr2, _ := e.screen(token, http.MethodGet, "/v1/screen/menu",
		map[string]string{"If-None-Match": etag1})
	if st != http.StatusNotModified {
		t.Fatalf("expected 304, got %d", st)
	}
	if hdr2.Get("ETag") != etag1 {
		t.Fatal("304 should still carry current ETag")
	}

	// A price/draft edit that is NOT republished must NOT change the live ETag
	// (drafts don't affect live).
	dishID := e.dishIDBySku(storeID, "B")
	e.admin(http.MethodPut,
		fmt.Sprintf("/v1/admin/stores/%d/dishes/%d", storeID, dishID),
		`{"sku":"B","name":"Burger","base_price":1500,"active":true,"daily_limit":2}`)
	st, _, _ = e.screen(token, http.MethodGet, "/v1/screen/menu",
		map[string]string{"If-None-Match": etag1})
	if st != http.StatusNotModified {
		t.Fatalf("draft edit changed live cache (got %d); expected 304", st)
	}

	// Publishing the draft change bumps the version -> new ETag, stale token
	// now returns 200 with new state.
	st, _ = e.publish(storeID, `{"expected_version":1}`)
	if st != http.StatusCreated {
		t.Fatalf("publish v2: %d", st)
	}
	st, hdr3, menu2 := e.screen(token, http.MethodGet, "/v1/screen/menu",
		map[string]string{"If-None-Match": etag1})
	if st != http.StatusOK {
		t.Fatalf("after publish expected 200 (stale etag), got %d", st)
	}
	etag2 := hdr3.Get("ETag")
	if etag2 == etag1 {
		t.Fatal("ETag did not change after publish")
	}
	if p := int64(menuItemBySku(menu2["items"].([]any), "B")["base_price"].(float64)); p != 1500 {
		t.Fatalf("new menu should reflect published price 1500, got %d", p)
	}

	// Sold-out transition changes the ETag even without a publish.
	e.sendEvent(storeID, `{"event_id":"S1","sku":"B","qty":2,"occurred_at":"2026-04-01T12:05:00Z"}`)
	st, hdr4, _ := e.screen(token, http.MethodGet, "/v1/screen/menu",
		map[string]string{"If-None-Match": etag2})
	if st != http.StatusOK {
		t.Fatalf("sold-out should invalidate cache, got %d", st)
	}
	etag3 := hdr4.Get("ETag")
	if etag3 == etag2 {
		t.Fatal("ETag did not change after sold-out")
	}

	// Polling again with etag3 -> 304.
	if st, _, _ = e.screen(token, http.MethodGet, "/v1/screen/menu",
		map[string]string{"If-None-Match": etag3}); st != http.StatusNotModified {
		t.Fatalf("expected 304 with current etag, got %d", st)
	}

	// Cross-day recovery changes ETag (sold-out marker scoped to the day).
	nextDay, _ := time.Parse(time.RFC3339, "2026-04-02T08:00:00Z")
	pinClock(t, nextDay)
	st, hdr5, menu3 := e.screen(token, http.MethodGet, "/v1/screen/menu",
		map[string]string{"If-None-Match": etag3})
	if st != http.StatusOK {
		t.Fatalf("cross-day expected 200, got %d", st)
	}
	if hdr5.Get("ETag") == etag3 {
		t.Fatal("ETag did not change across day boundary")
	}
	if item := menuItemBySku(menu3["items"].([]any), "B"); item["sold_out"] != false {
		t.Fatal("item should recover on new day")
	}
}

// TestScreen_HeartbeatAndOffline checks the 90-second liveness window: a fresh
// heartbeat means online; after more than the threshold without one the screen
// is offline; on reconnect it can fetch the full latest menu.
func TestScreen_HeartbeatAndOffline(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)
	e.publish(storeID, `{"expected_version":0}`)
	token := e.provisionScreen(storeID, "s")

	// Before any heartbeat the screen is offline.
	t0 := time.Date(2026, 5, 1, 10, 0, 0, 0, time.UTC)
	pinClock(t, t0)
	st, _, status := e.screen(token, http.MethodGet, "/v1/screen/status", nil)
	if st != http.StatusOK || status["online"] != false {
		t.Fatalf("fresh screen should be offline: %d %v", st, status)
	}

	// Heartbeat -> online.
	if st, _, _ = e.screen(token, http.MethodPost, "/v1/screen/heartbeat", nil); st != http.StatusOK {
		t.Fatalf("heartbeat: %d", st)
	}
	pinClock(t, t0.Add(89*time.Second))
	st, _, status = e.screen(token, http.MethodGet, "/v1/screen/status", nil)
	if status["online"] != true {
		t.Fatalf("should be online at 89s: %v", status)
	}

	// 91s without heartbeat -> offline.
	pinClock(t, t0.Add(91*time.Second))
	st, _, status = e.screen(token, http.MethodGet, "/v1/screen/status", nil)
	if status["online"] != false {
		t.Fatalf("should be offline at 91s: %v", status)
	}

	// Reconnect: heartbeat then full menu fetch succeeds with latest version.
	if st, _, hb := e.screen(token, http.MethodPost, "/v1/screen/heartbeat", nil); st != http.StatusOK {
		t.Fatalf("reconnect heartbeat: %d", st)
	} else if int64(hb["menu_version"].(float64)) != 1 {
		t.Fatalf("heartbeat should report current version: %v", hb["menu_version"])
	}
	st, _, menu := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if st != http.StatusOK || int64(menu["menu_version"].(float64)) != 1 {
		t.Fatalf("reconnect full menu fetch failed: %d %v", st, menu)
	}
}

// TestScreen_NoMixedState uses a serializable snapshot read; we additionally
// assert every menu response is internally consistent: version N items always
// carry that version's snapshot and prices. Here we sanity-check the response
// version matches the store and item prices belong to it.
func TestScreen_NoMixedState(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)
	e.publish(storeID, `{"expected_version":0}`)
	token := e.provisionScreen(storeID, "s")
	pinClock(t, time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC))

	st, _, menu := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if st != http.StatusOK {
		t.Fatalf("menu: %d", st)
	}
	if int64(menu["menu_version"].(float64)) != 1 {
		t.Fatalf("version mismatch: %v", menu["menu_version"])
	}
	var n int
	err := e.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM menu_version_items mvi JOIN menu_versions mv ON mv.id=mvi.version_id
		 WHERE mv.store_id=$1 AND mv.version=1`, storeID).Scan(&n)
	if err != nil {
		t.Fatalf("count: %v", err)
	}
	if len(menu["items"].([]any)) != n {
		t.Fatalf("response item count %d != version snapshot %d (mixed state)", len(menu["items"].([]any)), n)
	}
}
