package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"targetcraft/internal/httpapi"
	"targetcraft/internal/service"
	"targetcraft/internal/testsupport"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	gin.SetMode(gin.TestMode)
	svc := service.New(testsupport.OpenTestDB(t))
	svc.SetClock(func() time.Time {
		return time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	})
	return httptest.NewServer(httpapi.NewRouter(svc))
}

func post(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return resp.StatusCode, out
}

func TestDecideConfirmFlowOverHTTP(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	status, camp := post(t, srv.URL+"/api/v1/campaigns", map[string]any{
		"name":                "http-campaign",
		"start_at":            "2026-09-20T00:00:00Z",
		"end_at":              "2026-09-21T00:00:00Z",
		"total_budget_cents":  10000,
		"daily_cap_cents":     5000,
		"regions":             []string{"CN"},
		"devices":             []string{"ios"},
		"hour_windows":        []string{"00:00-23:59"},
	})
	if status != http.StatusOK {
		t.Fatalf("create campaign status=%d body=%v", status, camp)
	}
	campaignID := uint64(camp["id"].(float64))

	status, creative := post(t, srv.URL+"/api/v1/creatives", map[string]any{
		"campaign_id": campaignID,
		"name":        "http-creative",
	})
	if status != http.StatusOK {
		t.Fatalf("create creative status=%d body=%v", status, creative)
	}
	creativeID := uint64(creative["id"].(float64))

	decideBody := map[string]any{
		"request_id":  "http-req-1",
		"creative_id": creativeID,
		"user_id":     "user-1",
		"region":      "CN",
		"device":      "ios",
		"occurred_at": "2026-09-20T12:00:00Z",
		"cost_cents":  100,
	}
	status, dec := post(t, srv.URL+"/api/v1/decisions", decideBody)
	if status != http.StatusOK || dec["accepted"] != true {
		t.Fatalf("decide status=%d body=%v", status, dec)
	}

	// Idempotent replay returns the same accepted response.
	status, replay := post(t, srv.URL+"/api/v1/decisions", decideBody)
	if status != http.StatusOK || replay["accepted"] != true || replay["expires_at"] != dec["expires_at"] {
		t.Fatalf("replay status=%d body=%v", status, replay)
	}

	// Same request_id, different content -> 409.
	conflict := map[string]any{}
	for k, v := range decideBody {
		conflict[k] = v
	}
	conflict["cost_cents"] = 200
	status, body := post(t, srv.URL+"/api/v1/decisions", conflict)
	if status != http.StatusConflict {
		t.Fatalf("conflict status=%d body=%v", status, body)
	}

	// Confirm, then confirm again (idempotent).
	status, conf := post(t, srv.URL+"/api/v1/decisions/http-req-1/confirm", map[string]any{})
	if status != http.StatusOK || conf["status"] != "confirmed" {
		t.Fatalf("confirm status=%d body=%v", status, conf)
	}
	status, conf2 := post(t, srv.URL+"/api/v1/decisions/http-req-1/confirm", map[string]any{})
	if status != http.StatusOK || conf2["confirmed_at"] != conf["confirmed_at"] {
		t.Fatalf("duplicate confirm status=%d body=%v", status, conf2)
	}

	// Records are queryable.
	resp, err := http.Get(srv.URL + "/api/v1/decisions?user_id=user-1")
	if err != nil {
		t.Fatalf("list decisions: %v", err)
	}
	defer resp.Body.Close()
	var list struct {
		Total int `json:"total"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil || list.Total != 1 {
		t.Fatalf("list decisions total=%d err=%v", list.Total, err)
	}

	resp2, err := http.Get(srv.URL + "/api/v1/settlements?type=confirm")
	if err != nil {
		t.Fatalf("list settlements: %v", err)
	}
	defer resp2.Body.Close()
	var slist struct {
		Total int `json:"total"`
	}
	if err := json.NewDecoder(resp2.Body).Decode(&slist); err != nil || slist.Total != 1 {
		t.Fatalf("list settlements total=%d err=%v", slist.Total, err)
	}
}

func TestDecideRejectionOverHTTP(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	_, camp := post(t, srv.URL+"/api/v1/campaigns", map[string]any{
		"name":                "reject-campaign",
		"start_at":            "2026-09-20T00:00:00Z",
		"end_at":              "2026-09-21T00:00:00Z",
		"total_budget_cents":  10000,
		"daily_cap_cents":     5000,
		"regions":             []string{"CN"},
	})
	campaignID := uint64(camp["id"].(float64))
	_, creative := post(t, srv.URL+"/api/v1/creatives", map[string]any{
		"campaign_id": campaignID,
		"name":        "reject-creative",
	})

	// Business rejection is HTTP 200 with accepted:false and a reason.
	status, dec := post(t, srv.URL+"/api/v1/decisions", map[string]any{
		"request_id":  "http-req-region",
		"creative_id": creative["id"],
		"user_id":     "user-1",
		"region":      "US",
		"device":      "ios",
		"occurred_at": "2026-09-20T12:00:00Z",
		"cost_cents":  100,
	})
	if status != http.StatusOK || dec["accepted"] != false || dec["reject_code"] != "REGION_NOT_MATCHED" {
		t.Fatalf("reject decide status=%d body=%v", status, dec)
	}

	// Confirming a rejected decision is a 409.
	status, body := post(t, srv.URL+"/api/v1/decisions/http-req-region/confirm", map[string]any{})
	if status != http.StatusConflict {
		t.Fatalf("confirm rejected decision status=%d body=%v", status, body)
	}
}
