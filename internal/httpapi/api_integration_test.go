package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/detection"
	"anomalywatch/internal/httpapi"
	"anomalywatch/internal/ingest"
	"anomalywatch/internal/models"
	"anomalywatch/internal/testdb"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type harness struct {
	db       *gorm.DB
	cfg      config.Config
	router   *gin.Engine
	engine   *detection.Engine
	adminKey string
	analyst  string
	emps     map[string]uint64
}

func newHarness(t *testing.T) harness {
	t.Helper()
	gdb, cfg := testdb.New(t)
	cfg.MaxBackfillAge = 60 * 24 * time.Hour

	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(gin.Recovery())
	eng := detection.New(gdb, cfg)
	h := &httpapi.Handlers{
		DB:     gdb,
		Cfg:    cfg,
		Ingest: ingest.New(gdb, cfg),
		Engine: eng,
	}
	grp := r.Group("/")
	grp.Use(httpapi.Auth(gdb))
	h.Register(grp)

	return harness{
		db:       gdb,
		cfg:      cfg,
		router:   r,
		engine:   eng,
		adminKey: cfg.AdminKey,
		analyst:  cfg.AnalystKey,
		emps:     testdb.EmployeeIDs(t, gdb),
	}
}

func (h harness) do(t *testing.T, method, path, key string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("X-API-Key", key)
	}
	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, req)
	return w
}

func (h harness) postEvents(t *testing.T, key string, evs []map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	return h.do(t, http.MethodPost, "/api/v1/events/batch", key, map[string]any{
		"source": "test",
		"events": evs,
	})
}

// --- Auth -------------------------------------------------------------------

func TestAuthRequired(t *testing.T) {
	h := newHarness(t)
	w := h.do(t, http.MethodGet, "/api/v1/alerts", "", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("no key: code = %d, want 401", w.Code)
	}
	w = h.do(t, http.MethodGet, "/api/v1/alerts", "wrong-key", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("bad key: code = %d, want 401", w.Code)
	}
}

// --- Department-scoped RBAC --------------------------------------------------

func TestAnalystCannotAccessOtherDepartment(t *testing.T) {
	h := newHarness(t)
	// Analyst is bound to Engineering: Alice(1), Bob(2). Carol(3) is Finance.
	now := time.Now().UTC().Add(-2 * time.Hour)
	h.postEvents(t, h.adminKey, []map[string]any{
		{"event_id": "e-alice", "employee_id": h.emps["Alice Chen"], "event_type": "login", "occurred_at": now},
		{"event_id": "e-carol", "employee_id": h.emps["Carol Wang"], "event_type": "login", "occurred_at": now},
	})

	_ = h.do(t, http.MethodGet, "/api/v1/alerts", h.analyst, nil)
	// No alerts yet, but events endpoint proves the scope filter.
	we := h.do(t, http.MethodGet, "/api/v1/events?limit=200", h.analyst, nil)
	if we.Code != http.StatusOK {
		t.Fatalf("analyst list events: %d", we.Code)
	}
	var list struct {
		Items []models.Event `json:"items"`
	}
	if err := json.Unmarshal(we.Body.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	for _, ev := range list.Items {
		if ev.EmployeeID == h.emps["Carol Wang"] {
			t.Fatal("analyst saw a Finance employee's event")
		}
	}
	if len(list.Items) == 0 {
		t.Fatal("analyst should see Engineering events")
	}
}

func TestAnalystForbiddenFromAdminEndpoints(t *testing.T) {
	h := newHarness(t)
	cases := []struct{ method, path string }{
		{http.MethodPut, "/api/v1/rules/download_burst"},
		{http.MethodPut, "/api/v1/employees/1/timezone"},
		{http.MethodGet, "/api/v1/audit"},
		{http.MethodPost, "/admin/jobs/sweep"},
	}
	for _, c := range cases {
		var body any
		if c.method == http.MethodPut {
			body = map[string]any{"time_zone": "UTC"}
		}
		w := h.do(t, c.method, c.path, h.analyst, body)
		if w.Code != http.StatusForbidden {
			t.Fatalf("%s %s as analyst: code = %d, want 403", c.method, c.path, w.Code)
		}
	}
}

func TestAnalystCannotReadOtherDeptAlertDirectly(t *testing.T) {
	h := newHarness(t)
	// Create a night alert for Carol (Finance) directly.
	carol := h.emps["Carol Wang"]
	ws := time.Date(2026, 9, 20, 2, 0, 0, 0, time.UTC) // 03:00 London, night
	alert := models.Alert{
		RuleCode: models.RuleNightActivity, RuleVersion: 1, EmployeeID: carol,
		Status: models.AlertStatusNew, Severity: "medium", Title: "x",
		DedupKey: "night:finance-fixture", Evidence: models.JSONMap{},
		FiredAt: time.Now().Add(-25 * time.Hour), UpdatedAt: time.Now(),
		WindowStart: &ws,
	}
	if err := h.db.Create(&alert).Error; err != nil {
		t.Fatal(err)
	}
	w := h.do(t, http.MethodGet, "/api/v1/alerts/"+itoa(alert.ID), h.analyst, nil)
	if w.Code != http.StatusNotFound {
		t.Fatalf("analyst cross-dept read: code = %d, want 404", w.Code)
	}
	w = h.do(t, http.MethodPost, "/api/v1/alerts/"+itoa(alert.ID)+"/transition", h.analyst,
		map[string]any{"status": "resolved"})
	if w.Code != http.StatusNotFound {
		t.Fatalf("analyst cross-dept transition: code = %d, want 404", w.Code)
	}
	// Admin can read it.
	w = h.do(t, http.MethodGet, "/api/v1/alerts/"+itoa(alert.ID), h.adminKey, nil)
	if w.Code != http.StatusOK {
		t.Fatalf("admin read: code = %d, want 200", w.Code)
	}
}

// --- Alert lifecycle ---------------------------------------------------------

func TestAlertTransitionsAndAudit(t *testing.T) {
	h := newHarness(t)
	alert := models.Alert{
		RuleCode: models.RuleFirstUSB, RuleVersion: 1, EmployeeID: h.emps["Alice Chen"],
		Status: models.AlertStatusNew, Severity: "high", Title: "usb",
		DedupKey: "usb:lifecycle", Evidence: models.JSONMap{},
		FiredAt: time.Now().UTC(), UpdatedAt: time.Now().UTC(),
	}
	if err := h.db.Create(&alert).Error; err != nil {
		t.Fatal(err)
	}
	transition := func(status string, want int) {
		t.Helper()
		w := h.do(t, http.MethodPost, "/api/v1/alerts/"+itoa(alert.ID)+"/transition", h.adminKey,
			map[string]any{"status": status, "note": "investigating now"})
		if w.Code != want {
			t.Fatalf("-> %s: code = %d body = %s, want %d", status, w.Code, w.Body.String(), want)
		}
	}
	transition("investigating", http.StatusOK)
	transition("escalated", http.StatusOK)
	transition("resolved", http.StatusOK)
	// Terminal: no outgoing transitions.
	transition("investigating", http.StatusConflict)

	var logs []models.AuditLog
	h.db.Where("entity_type = ? AND entity_id = ? AND action = ?", "alert", itoa(alert.ID), "alert.transition").Order("id").Find(&logs)
	if len(logs) != 3 {
		t.Fatalf("audit rows = %d, want 3", len(logs))
	}
	wantFrom := []string{"new", "investigating", "escalated"}
	for i, l := range logs {
		if l.Detail["from"] != wantFrom[i] {
			t.Fatalf("audit[%d].from = %v, want %s (detail=%v)", i, l.Detail["from"], wantFrom[i], l.Detail)
		}
	}
}

// --- 24h auto-escalation -----------------------------------------------------

func TestOverdueNewAlertAutoEscalates(t *testing.T) {
	h := newHarness(t)
	old := time.Now().UTC().Add(-25 * time.Hour)
	alert := models.Alert{
		RuleCode: models.RuleFirstUSB, RuleVersion: 1, EmployeeID: h.emps["Alice Chen"],
		Status: models.AlertStatusNew, Severity: "high", Title: "old",
		DedupKey: "usb:old", Evidence: models.JSONMap{},
		FiredAt: old, UpdatedAt: old,
	}
	if err := h.db.Create(&alert).Error; err != nil {
		t.Fatal(err)
	}
	n, err := h.Engine().EscalateOverdue()
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("escalated = %d, want 1", n)
	}
	var reloaded models.Alert
	h.db.First(&reloaded, alert.ID)
	if reloaded.Status != models.AlertStatusEscalated || reloaded.EscalatedAt == nil {
		t.Fatalf("status = %s escalated_at = %v", reloaded.Status, reloaded.EscalatedAt)
	}
	// Idempotent: a second run touches nothing.
	n2, err := h.Engine().EscalateOverdue()
	if err != nil || n2 != 0 {
		t.Fatalf("second escalate = (%d,%v), want (0,nil)", n2, err)
	}
}

// --- Rules and timezones ------------------------------------------------------

func TestAdminRuleUpdateBumpsVersionAndAudits(t *testing.T) {
	h := newHarness(t)
	w := h.do(t, http.MethodPut, "/api/v1/rules/download_burst", h.adminKey, map[string]any{
		"params": map[string]any{"window_minutes": 15, "threshold": 30},
	})
	if w.Code != http.StatusOK {
		t.Fatalf("update rule: %d %s", w.Code, w.Body.String())
	}
	var r models.Rule
	h.db.Where("code = ?", models.RuleDownloadBurst).First(&r)
	if r.Version != 2 {
		t.Fatalf("version = %d, want 2", r.Version)
	}
	if got := int(r.Params["threshold"].(float64)); got != 30 {
		t.Fatalf("threshold = %v, want 30", r.Params["threshold"])
	}
	var n int64
	h.db.Model(&models.AuditLog{}).Where("action = ? AND entity_id = ?", "rule.update", "download_burst").Count(&n)
	if n != 1 {
		t.Fatalf("rule audit rows = %d, want 1", n)
	}
	// Invalid params are rejected (version must not bump).
	w = h.do(t, http.MethodPut, "/api/v1/rules/download_burst", h.adminKey, map[string]any{
		"params": map[string]any{"window_minutes": -5, "threshold": 30},
	})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("invalid params: code = %d, want 400", w.Code)
	}
}

func TestAdminTimezoneUpdateAuditsAndInvalidates(t *testing.T) {
	h := newHarness(t)
	alice := h.emps["Alice Chen"]
	// Seed a night evaluation row.
	h.db.Exec(`INSERT INTO window_evaluations
		(rule_code, rule_version, employee_id, window_scope, window_date, window_start, window_end, evaluated_at, had_alert)
		VALUES ('night_activity', 1, ?, 'night', '2026-09-20', '2026-09-20 12:00:00', '2026-09-21 10:00:00', UTC_TIMESTAMP(3), 1)`, alice)

	w := h.do(t, http.MethodPut, "/api/v1/employees/"+itoa(alice)+"/timezone", h.adminKey,
		map[string]any{"time_zone": "Europe/Berlin"})
	if w.Code != http.StatusOK {
		t.Fatalf("update tz: %d %s", w.Code, w.Body.String())
	}
	var emp models.Employee
	h.db.First(&emp, alice)
	if emp.TimeZone != "Europe/Berlin" {
		t.Fatalf("tz = %s", emp.TimeZone)
	}
	var n int64
	h.db.Model(&models.WindowEvaluation{}).
		Where("employee_id = ? AND window_scope = ?", alice, models.ScopeNight).Count(&n)
	if n != 0 {
		t.Fatalf("night evaluations after tz change = %d, want 0 (recomputed on next sweep)", n)
	}
	var audits int64
	h.db.Model(&models.AuditLog{}).Where("action = ?", "employee.timezone").Count(&audits)
	if audits != 1 {
		t.Fatalf("tz audit rows = %d, want 1", audits)
	}
	w = h.do(t, http.MethodPut, "/api/v1/employees/"+itoa(alice)+"/timezone", h.adminKey,
		map[string]any{"time_zone": "Not/AZone"})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("bad tz: code = %d, want 400", w.Code)
	}
}

// --- Batch validation through the real router --------------------------------

func TestBatchConflictReturns409(t *testing.T) {
	h := newHarness(t)
	at := time.Now().UTC().Add(-time.Hour).Format(time.RFC3339)
	ev := func(when string) map[string]any {
		return map[string]any{"event_id": "c1", "employee_id": h.emps["David Zhao"], "event_type": "login", "occurred_at": when}
	}
	w := h.postEvents(t, h.adminKey, []map[string]any{ev(at)})
	if w.Code != http.StatusAccepted {
		t.Fatalf("first: %d %s", w.Code, w.Body.String())
	}
	w = h.postEvents(t, h.adminKey, []map[string]any{
		ev(time.Now().UTC().Add(-2 * time.Hour).Format(time.RFC3339)),
	})
	if w.Code != http.StatusConflict {
		t.Fatalf("conflict: code = %d body = %s, want 409", w.Code, w.Body.String())
	}
	var body struct {
		Result struct {
			Items []struct {
				Status string `json:"status"`
			} `json:"items"`
		} `json:"result"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Result.Items) != 1 || body.Result.Items[0].Status != "conflict" {
		t.Fatalf("items = %+v, want one conflict", body.Result.Items)
	}
}

func TestBatchSizeLimitReturns400(t *testing.T) {
	h := newHarness(t)
	var evs []map[string]any
	for i := 0; i < h.cfg.MaxEventsPerBatch+1; i++ {
		evs = append(evs, map[string]any{
			"event_id":    "big-" + itoa(uint64(i)),
			"employee_id": h.emps["David Zhao"],
			"event_type":  "login",
			"occurred_at": time.Now().UTC().Add(-time.Hour).Format(time.RFC3339),
		})
	}
	w := h.postEvents(t, h.adminKey, evs)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("oversized batch: code = %d, want 400", w.Code)
	}
}
