package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	httpapi "signalboard/internal/httpapi"
	"signalboard/internal/service"
	"signalboard/internal/testsupport"
)

const apiKey = "test-mgmt-key"

type harness struct {
	*testsupport.Env
	srv *httptest.Server
}

func newHarness(t *testing.T) *harness {
	e := testsupport.New(t)
	srv := httptest.NewServer(httpapi.NewServer(e.Svc, apiKey))
	t.Cleanup(srv.Close)
	return &harness{Env: e, srv: srv}
}

func (h *harness) request(t *testing.T, method, path, token string, body any, headers map[string]string) *http.Response {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, h.srv.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func (h *harness) do(t *testing.T, method, path, token string, body any) (int, http.Header, []byte) {
	t.Helper()
	resp := h.request(t, method, path, token, body, nil)
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, resp.Header, data
}

func wantStatus(t *testing.T, got, want int, body []byte) {
	t.Helper()
	if got != want {
		t.Fatalf("status = %d, want %d; body=%s", got, want, string(body))
	}
}

func decodeJSON(t *testing.T, b []byte, v any) {
	t.Helper()
	if err := json.Unmarshal(b, v); err != nil {
		t.Fatalf("decode %s: %v", string(b), err)
	}
}

func setupStoreWithMenu(t *testing.T, h *harness) (storeID int64, dishIDs []int64, screenToken string) {
	ctx := context.Background()
	st, err := h.Svc.CreateStore(ctx, "Burgers", "UTC")
	if err != nil {
		t.Fatal(err)
	}
	for i, name := range []string{"Burger", "Fries"} {
		d, err := h.Svc.CreateDish(ctx, st.ID, name, int64(500+i*50))
		if err != nil {
			t.Fatal(err)
		}
		dishIDs = append(dishIDs, d.ID)
	}
	h.DraftAndPublish(t, st.ID, dishIDs, 0)
	_, token, err := h.Svc.RegisterScreen(ctx, st.ID, "s1")
	if err != nil {
		t.Fatal(err)
	}
	return st.ID, dishIDs, token
}

func TestAuthRequired(t *testing.T) {
	h := newHarness(t)

	if status, _, _ := h.do(t, "GET", "/v1/stores", "", nil); status != http.StatusUnauthorized {
		t.Fatalf("mgmt without key: status=%d want 401", status)
	}
	if status, _, _ := h.do(t, "GET", "/screen/v1/menu", "", nil); status != http.StatusUnauthorized {
		t.Fatalf("screen without token: status=%d want 401", status)
	}
	if status, _, _ := h.do(t, "GET", "/v1/stores", "wrong-key", nil); status != http.StatusUnauthorized {
		t.Fatalf("mgmt bad key: status=%d want 401", status)
	}
}

// A screen token only ever sees its own store; there is no store parameter on
// the screen endpoint, so a token cannot be used to read another store.
func TestScreenScopedToOwnStore(t *testing.T) {
	h := newHarness(t)
	storeA, dishesA, tokenA := setupStoreWithMenu(t, h)
	_, dishesB, tokenB := setupStoreWithMenu(t, h)

	status, hdr, body := h.do(t, "GET", "/screen/v1/menu", tokenA, nil)
	wantStatus(t, status, http.StatusOK, body)
	if got := hdr.Get("X-Store-Id"); got != strconv.FormatInt(storeA, 10) {
		t.Fatalf("X-Store-Id = %s, want %d", got, storeA)
	}
	var menuA service.ScreenMenu
	decodeJSON(t, body, &menuA)
	if menuA.StoreID != storeA || len(menuA.Lines) != len(dishesA) {
		t.Fatalf("screen A saw wrong store/menu: %+v", menuA)
	}

	status, _, body = h.do(t, "GET", "/screen/v1/menu", tokenB, nil)
	wantStatus(t, status, http.StatusOK, body)
	var menuB service.ScreenMenu
	decodeJSON(t, body, &menuB)
	if menuB.StoreID == storeA {
		t.Fatal("screen B resolved to store A")
	}
	if len(menuB.Lines) != len(dishesB) {
		t.Fatalf("screen B saw %d lines, want %d", len(menuB.Lines), len(dishesB))
	}
}

// Full ETag lifecycle: 304 on unchanged, 200+new ETag on publish / price /
// sellout changes, 412 on stale If-Match.
func TestETagConditionalAndCacheInvalidation(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	storeID, dishIDs, token := setupStoreWithMenu(t, h)

	get := func(hdrs map[string]string) (int, string, service.ScreenMenu, []byte) {
		resp := h.request(t, "GET", "/screen/v1/menu", token, nil, hdrs)
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		var menu service.ScreenMenu
		if len(b) > 0 {
			decodeJSON(t, b, &menu)
		}
		return resp.StatusCode, resp.Header.Get("ETag"), menu, b
	}

	status, etag1, menu1, _ := get(nil)
	wantStatus(t, status, http.StatusOK, nil)
	if etag1 == "" {
		t.Fatal("missing ETag")
	}
	if menu1.Lines[0].Price != 500 {
		t.Fatalf("initial price = %d, want 500", menu1.Lines[0].Price)
	}

	// If-None-Match current -> 304, no body.
	st, etag, _, b := get(map[string]string{"If-None-Match": etag1})
	if st != http.StatusNotModified {
		wantStatus(t, st, http.StatusNotModified, b)
	}
	if etag != etag1 || len(b) != 0 {
		t.Fatal("304 must carry same ETag and empty body")
	}

	// Stale If-Match -> 412.
	stale := `"0000000000000000000000000000000000000000000000000000000000000000"`
	if st, _, _, b = get(map[string]string{"If-Match": stale}); st != http.StatusPreconditionFailed {
		wantStatus(t, st, http.StatusPreconditionFailed, b)
	}

	// Publish changes state -> stale INM is a normal 200 with new ETag.
	if _, err := h.Svc.ReplaceDraft(ctx, storeID,
		[]service.DraftItemInput{{DishID: dishIDs[0]}}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Svc.Publish(ctx, storeID, 1, "trim", nil); err != nil {
		t.Fatal(err)
	}
	st, etag2, menu2, b := get(map[string]string{"If-None-Match": etag1})
	wantStatus(t, st, http.StatusOK, b)
	if etag2 == etag1 {
		t.Fatal("ETag unchanged after publish")
	}
	if len(menu2.Lines) != 1 {
		t.Fatalf("stale cache served after publish: %d lines", len(menu2.Lines))
	}

	// Temp price activation (time-band now) changes ETag even though no publish
	// happens between requests.
	now := time.Now().UTC()
	if _, err := h.Svc.ScheduleTempPrices(ctx, storeID, []service.TempPriceInput{
		{DishID: dishIDs[0], Price: 111, StartsAt: now.Add(-time.Minute), EndsAt: now.Add(time.Minute)},
	}); err != nil {
		t.Fatal(err)
	}
	st, etag3, menu3, b := get(map[string]string{"If-None-Match": etag2})
	wantStatus(t, st, http.StatusOK, b)
	if etag3 == etag2 {
		t.Fatal("ETag unchanged after active temp price")
	}
	if menu3.Lines[0].Price != 111 {
		t.Fatalf("temp price not applied to screen: %d", menu3.Lines[0].Price)
	}

	// Sellout flip changes ETag too.
	if err := h.Svc.SetThreshold(ctx, storeID, dishIDs[0], 1); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Svc.IngestSalesEvent(ctx, storeID, service.SalesEventInput{
		EventID: "s1", DishID: dishIDs[0], Quantity: 1, OccurredAt: time.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	st, etag4, menu4, b := get(map[string]string{"If-None-Match": etag3})
	wantStatus(t, st, http.StatusOK, b)
	if etag4 == etag3 {
		t.Fatal("ETag unchanged after sellout")
	}
	if !menu4.Lines[0].SoldOut {
		t.Fatal("sold_out not reflected")
	}

	// Fresh validator against current state -> 304.
	if st, _, _, b = get(map[string]string{"If-None-Match": etag4}); st != http.StatusNotModified {
		wantStatus(t, st, http.StatusNotModified, b)
	}
}

// Heartbeat keeps the screen online; the management list reflects 90s liveness.
func TestHeartbeatAndOnlineStatus(t *testing.T) {
	h := newHarness(t)
	storeID, _, token := setupStoreWithMenu(t, h)

	if status, _, b := h.do(t, "POST", "/screen/v1/heartbeat", token, nil); status != http.StatusOK {
		wantStatus(t, status, http.StatusOK, b)
	}
	status, _, body := h.do(t, "GET",
		"/v1/stores/"+strconv.FormatInt(storeID, 10)+"/screens", apiKey, nil)
	wantStatus(t, status, http.StatusOK, body)
	var screens []service.ScreenStatus
	decodeJSON(t, body, &screens)
	if len(screens) != 1 || !screens[0].Online {
		t.Fatalf("screen should be online after heartbeat: %+v", screens)
	}
}

// The full menu endpoint is what a reconnecting screen calls; it always
// returns a complete snapshot (never an incremental/delta response).
func TestReconnectReturnsFullMenu(t *testing.T) {
	h := newHarness(t)
	_, dishIDs, token := setupStoreWithMenu(t, h)
	status, _, body := h.do(t, "GET", "/screen/v1/menu", token, nil)
	wantStatus(t, status, http.StatusOK, body)
	var menu service.ScreenMenu
	decodeJSON(t, body, &menu)
	if len(menu.Lines) != len(dishIDs) || menu.Version != 1 {
		t.Fatalf("reconnect menu incomplete: %+v", menu)
	}
}
