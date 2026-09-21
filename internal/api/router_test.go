package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"targetcraft/internal/models"
	"targetcraft/internal/service"
	"targetcraft/internal/store"
)

var testRouter *gin.Engine

func TestMain(m *testing.M) {
	dsn := os.Getenv("TARGETCRAFT_TEST_API_DSN")
	if dsn == "" {
		dsn = "root@tcp(127.0.0.1:3306)/?charset=utf8mb4&parseTime=true&loc=UTC"
	}
	admin, err := gorm.Open(mysql.Open(dsn), &gorm.Config{Logger: gormlogger.Default.LogMode(gormlogger.Silent)})
	if err != nil {
		fmt.Fprintf(os.Stderr, "SKIP: cannot connect to MySQL for api tests: %v\n", err)
		os.Exit(0)
	}
	dbName := "targetcraft_api_test"
	_ = admin.Exec("DROP DATABASE IF EXISTS " + dbName).Error
	if err := admin.Exec("CREATE DATABASE " + dbName + " CHARACTER SET utf8mb4").Error; err != nil {
		fmt.Fprintf(os.Stderr, "create test db: %v\n", err)
		os.Exit(1)
	}
	testDSN := os.Getenv("TARGETCRAFT_TEST_API_DSN")
	if testDSN == "" {
		testDSN = "root@tcp(127.0.0.1:3306)/" + dbName + "?charset=utf8mb4&parseTime=true&loc=UTC"
	}
	db, err := store.Open(testDSN)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open test db: %v\n", err)
		os.Exit(1)
	}
	if err := store.Migrate(db); err != nil {
		fmt.Fprintf(os.Stderr, "migrate: %v\n", err)
		os.Exit(1)
	}

	gin.SetMode(gin.TestMode)
	svc := service.New(db, 5*time.Minute)
	testRouter = NewRouter(svc, db)

	code := m.Run()
	_ = admin.Exec("DROP DATABASE IF EXISTS " + dbName).Error
	os.Exit(code)
}

func doJSON(t *testing.T, method, path string, body any) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		reader = bytes.NewReader(b)
	} else {
		reader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, reader)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	testRouter.ServeHTTP(w, req)
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w, out
}

func TestDecisionFlowOverHTTP(t *testing.T) {
	now := time.Now().UTC()

	// Create campaign.
	w, resp := doJSON(t, "POST", "/api/campaigns", map[string]any{
		"name":         "http-campaign",
		"start_at":     now.Add(-time.Hour),
		"end_at":       now.Add(24 * time.Hour),
		"total_budget": 10000,
		"daily_cap":    5000,
		"rules":        map[string]any{"regions": []string{"CN"}, "devices": []string{"ios"}},
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("create campaign: %d %s", w.Code, w.Body)
	}
	campaignID := uint64(resp["campaign"].(map[string]any)["id"].(float64))

	// Create creative.
	w, resp = doJSON(t, "POST", fmt.Sprintf("/api/campaigns/%d/creatives", campaignID), map[string]any{
		"name": "banner", "content": "hello",
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("create creative: %d %s", w.Code, w.Body)
	}

	// Decision request: approved.
	decisionReq := map[string]any{
		"request_id":  "http-req-1",
		"user_id":     "http-user",
		"campaign_id": campaignID,
		"region":      "CN",
		"device":      "ios",
		"occurred_at": now,
		"cost":        100,
	}
	w, resp = doJSON(t, "POST", "/api/decisions", decisionReq)
	if w.Code != http.StatusCreated || resp["approved"] != true {
		t.Fatalf("decide: %d %s", w.Code, w.Body)
	}

	// Idempotent replay.
	w, resp = doJSON(t, "POST", "/api/decisions", decisionReq)
	if w.Code != http.StatusCreated || resp["approved"] != true {
		t.Fatalf("replay: %d %s", w.Code, w.Body)
	}

	// Same ID, different payload: 409.
	conflicting := map[string]any{}
	for k, v := range decisionReq {
		conflicting[k] = v
	}
	conflicting["cost"] = 200
	w, _ = doJSON(t, "POST", "/api/decisions", conflicting)
	if w.Code != http.StatusConflict {
		t.Fatalf("conflict: %d %s", w.Code, w.Body)
	}

	// Rule mismatch: rejected with reason.
	badRegion := map[string]any{}
	for k, v := range decisionReq {
		badRegion[k] = v
	}
	badRegion["request_id"] = "http-req-2"
	badRegion["region"] = "US"
	w, resp = doJSON(t, "POST", "/api/decisions", badRegion)
	if w.Code != http.StatusOK || resp["approved"] != false || resp["reason"] != "region_not_targeted" {
		t.Fatalf("rule reject: %d %s", w.Code, w.Body)
	}

	// Confirm the reservation.
	w, resp = doJSON(t, "POST", "/api/decisions/http-req-1/confirm", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("confirm: %d %s", w.Code, w.Body)
	}
	settlement := resp["settlement"].(map[string]any)
	if settlement["status"] != models.DecisionStatusConfirmed {
		t.Fatalf("settlement status: %v", settlement["status"])
	}

	// Query decisions and settlements.
	w, resp = doJSON(t, "GET", fmt.Sprintf("/api/decisions?campaign_id=%d", campaignID), nil)
	if w.Code != http.StatusOK || len(resp["decisions"].([]any)) == 0 {
		t.Fatalf("list decisions: %d %s", w.Code, w.Body)
	}
	w, resp = doJSON(t, "GET", fmt.Sprintf("/api/settlements?campaign_id=%d", campaignID), nil)
	if w.Code != http.StatusOK || len(resp["settlements"].([]any)) != 1 {
		t.Fatalf("list settlements: %d %s", w.Code, w.Body)
	}

	// Pause campaign: new decisions rejected.
	w, _ = doJSON(t, "POST", fmt.Sprintf("/api/campaigns/%d/pause", campaignID), nil)
	if w.Code != http.StatusOK {
		t.Fatalf("pause: %d %s", w.Code, w.Body)
	}
	pausedReq := map[string]any{}
	for k, v := range decisionReq {
		pausedReq[k] = v
	}
	pausedReq["request_id"] = "http-req-3"
	w, resp = doJSON(t, "POST", "/api/decisions", pausedReq)
	if w.Code != http.StatusOK || resp["reason"] != "campaign_not_active" {
		t.Fatalf("paused decide: %d %s", w.Code, w.Body)
	}
}
