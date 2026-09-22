package integration_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"activityguard/internal/api"
	"activityguard/internal/auth"
	"activityguard/internal/detection"
	"activityguard/internal/models"
	"activityguard/internal/testsupport"
)

// newHTTPServer 构造直接使用测试库的 HTTP 服务，绕过登录签发测试 JWT。
func newHTTPServer(t *testing.T, env *testsupport.Env) (*httptest.Server, func(string) string) {
	t.Helper()
	engine := detection.New(env.GDB, env.Cfg)
	srv := api.NewServer(env.GDB, env.Cfg, engine)
	ts := httptest.NewServer(srv.Router())
	t.Cleanup(ts.Close)

	tokenFor := func(username string) string {
		u := env.UserByUsername(t, username)
		tok, _, err := auth.IssueToken(env.Cfg.JWTSecret, time.Hour, u.ID, u.Username, u.Role)
		if err != nil {
			t.Fatalf("issue token: %v", err)
		}
		return tok
	}
	return ts, tokenFor
}

func doJSON(t *testing.T, method, url, token string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, url, rdr)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http: %v", err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	dec := json.NewDecoder(resp.Body)
	_ = dec.Decode(&out) // 错误响应也可能是 JSON
	return resp.StatusCode, out
}

// TestConcurrentImportDedup 多个 goroutine 并发上报同一批 event_id：
// 每个 event_id 必须且只能入账一次。
func TestConcurrentImportDedup(t *testing.T) {
	env := testsupport.New(t)
	ts, tokenFor := newHTTPServer(t, env)
	token := tokenFor("admin")
	emp := env.EmployeeByEmail(t, "david.kim@example.com")

	const workers = 8
	const perWorker = 50 // 每个 worker 上报同一批 50 个事件
	now := time.Now().UTC().Add(-2 * time.Hour)
	payload := map[string]any{"events": []map[string]any{}}
	evs := payload["events"].([]map[string]any)
	for i := 0; i < perWorker; i++ {
		evs = append(evs, map[string]any{
			"event_id":    fmt.Sprintf("cc-ev-%04d", i),
			"event_type":  "login",
			"employee_id": emp.ID,
			"occurred_at": now.Add(time.Duration(i) * time.Minute).Format(time.RFC3339Nano),
			"metadata":    map[string]any{"worker_index_hint": "same-content"},
		})
	}
	payload["events"] = evs

	var wg sync.WaitGroup
	statusCounts := map[int]int{}
	var mu sync.Mutex
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			status, body := doJSON(t, http.MethodPost, ts.URL+"/api/v1/events/batch", token, payload)
			mu.Lock()
			statusCounts[status]++
			mu.Unlock()
			if status != http.StatusOK {
				t.Errorf("worker got status %d: %v", status, body)
			}
		}()
	}
	wg.Wait()

	var n int64
	env.GDB.Model(&models.Event{}).Where("event_id LIKE ?", "cc-ev-%").Count(&n)
	if n != perWorker {
		t.Fatalf("persisted events = %d, want exactly %d after concurrent duplicate import", n, perWorker)
	}
	if statusCounts[http.StatusOK] != workers {
		t.Fatalf("status counts = %v", statusCounts)
	}

	// 总 accepted 应为 perWorker，其余全部 duplicate。
	var totalAccepted, totalDup int
	// 重新顺序送一次验证幂等返回。
	_, body := doJSON(t, http.MethodPost, ts.URL+"/api/v1/events/batch", token, payload)
	if ac, _ := body["accepted_count"].(float64); ac != 0 {
		t.Fatalf("third import accepted_count = %v, want 0", body["accepted_count"])
	}
	if dc, _ := body["duplicate_count"].(float64); int(dc) != perWorker {
		t.Fatalf("third import duplicate_count = %v, want %d", body["duplicate_count"], perWorker)
	}
	_ = totalAccepted
	_ = totalDup
}

// TestSameIDDifferentContentConflict 同 event_id 不同内容返回 409，且冲突批次不写入任何事件。
func TestSameIDDifferentContentConflict(t *testing.T) {
	env := testsupport.New(t)
	ts, tokenFor := newHTTPServer(t, env)
	token := tokenFor("admin")
	emp := env.EmployeeByEmail(t, "david.kim@example.com")
	t0 := time.Now().UTC().Add(-time.Hour)

	batch := func(metaVal string, extraID string) map[string]any {
		evs := []map[string]any{{
			"event_id": "conflict-id-1", "event_type": "usb",
			"employee_id": emp.ID, "occurred_at": t0.Format(time.RFC3339Nano),
			"metadata": map[string]any{"serial": metaVal},
		}}
		if extraID != "" {
			evs = append(evs, map[string]any{
				"event_id": extraID, "event_type": "login",
				"employee_id": emp.ID, "occurred_at": t0.Add(time.Minute).Format(time.RFC3339Nano),
			})
		}
		return map[string]any{"events": evs}
	}

	status, _ := doJSON(t, http.MethodPost, ts.URL+"/api/v1/events/batch", token, batch("SN-A", ""))
	if status != http.StatusOK {
		t.Fatalf("first batch status = %d, want 200", status)
	}
	status, body := doJSON(t, http.MethodPost, ts.URL+"/api/v1/events/batch", token, batch("SN-B-DIFFERENT", "should-not-persist-on-conflict"))
	if status != http.StatusConflict {
		t.Fatalf("conflict status = %d, want 409", status)
	}
	items, _ := body["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("conflict items = %v", items)
	}

	var n int64
	env.GDB.Model(&models.Event{}).Where("event_id = ?", "should-not-persist-on-conflict").Count(&n)
	if n != 0 {
		t.Fatalf("conflict batch must be atomic, but extra event persisted %d rows", n)
	}
}

// TestBatchLimits 超过 2000 条拒绝。
func TestBatchLimits(t *testing.T) {
	env := testsupport.New(t)
	ts, tokenFor := newHTTPServer(t, env)
	token := tokenFor("admin")
	emp := env.EmployeeByEmail(t, "david.kim@example.com")
	t0 := time.Now().UTC().Add(-30 * time.Minute)

	evs := make([]map[string]any, 0, 2001)
	for i := 0; i < 2001; i++ {
		evs = append(evs, map[string]any{
			"event_id":    fmt.Sprintf("limit-%05d", i),
			"event_type":  "login",
			"employee_id": emp.ID,
			"occurred_at": t0.Add(time.Duration(i) * time.Second).Format(time.RFC3339Nano),
		})
	}
	status, body := doJSON(t, http.MethodPost, ts.URL+"/api/v1/events/batch", token, map[string]any{"events": evs})
	if status != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", status)
	}
	if limit, _ := body["limit"].(float64); int(limit) != 2000 {
		t.Fatalf("limit = %v", body["limit"])
	}
}

// TestAnalystDepartmentScoping 分析员只能看被分配部门；越权访问统一 404；
// 无部门分配的分析员看到空列表；管理员可见全部。
func TestAnalystDepartmentScoping(t *testing.T) {
	env := testsupport.New(t)
	ts, tokenFor := newHTTPServer(t, env)

	// 先在两个部门制造告警：eng 与 sales 各一条夜间登录。
	engEmp := env.EmployeeByEmail(t, "bob.li@example.com")      // dept d1
	salesEmp := env.EmployeeByEmail(t, "david.kim@example.com") // dept d2
	engine := env.Engine()
	t0 := time.Now().UTC().Add(-30 * time.Minute)
	locSH, _ := time.LoadLocation("Asia/Shanghai")
	locKR, _ := time.LoadLocation("Asia/Seoul")
	nightSH := time.Date(t0.In(locSH).Year(), t0.In(locSH).Month(), t0.In(locSH).Day()-1, 23, 0, 0, 0, locSH)
	nightKR := time.Date(t0.In(locKR).Year(), t0.In(locKR).Month(), t0.In(locKR).Day()-1, 23, 30, 0, 0, locKR)
	if _, err := engine.ProcessEvents([]models.Event{
		testsupport.NewEvent("scope-eng-night", models.EventLogin, engEmp.ID, nightSH, nil),
		testsupport.NewEvent("scope-sales-night", models.EventLogin, salesEmp.ID, nightKR, nil),
	}); err != nil {
		t.Fatalf("process: %v", err)
	}
	var allAlerts []models.Alert
	if err := env.GDB.Find(&allAlerts).Error; err != nil {
		t.Fatalf("load alerts: %v", err)
	}
	alertByDept := map[string]models.Alert{}
	for _, a := range allAlerts {
		alertByDept[a.DepartmentID] = a
	}
	engAlert := alertByDept["000000000000000000000000d1"]
	salesAlert := alertByDept["000000000000000000000000d2"]

	// analyst_eng 仅分配 Engineering。
	engToken := tokenFor("analyst_eng")

	status, body := doJSON(t, http.MethodGet, ts.URL+"/api/v1/alerts", engToken, nil)
	if status != http.StatusOK {
		t.Fatalf("list status = %d", status)
	}
	items, _ := body["items"].([]any)
	for _, it := range items {
		m := it.(map[string]any)
		if m["department_id"] != "000000000000000000000000d1" {
			t.Fatalf("analyst_eng saw alert from department %v", m["department_id"])
		}
	}
	if len(items) == 0 {
		t.Fatal("analyst_eng should see engineering alerts")
	}

	// 直接访问 sales 告警 -> 404（不暴露存在性）。
	status, _ = doJSON(t, http.MethodGet, ts.URL+"/api/v1/alerts/"+salesAlert.ID, engToken, nil)
	if status != http.StatusNotFound {
		t.Fatalf("cross-dept GET status = %d, want 404", status)
	}

	// 对越权告警做状态流转 -> 同样 404。
	status, _ = doJSON(t, http.MethodPost,
		ts.URL+"/api/v1/alerts/"+salesAlert.ID+"/transition",
		engToken, map[string]any{"status": "investigating", "note": "hack"})
	if status != http.StatusNotFound {
		t.Fatalf("cross-dept transition status = %d, want 404", status)
	}

	// 用查询参数指定非授权部门 -> 403。
	status, _ = doJSON(t, http.MethodGet, ts.URL+"/api/v1/alerts?department_id=000000000000000000000000d2", engToken, nil)
	if status != http.StatusForbidden {
		t.Fatalf("filter other dept status = %d, want 403", status)
	}

	// analyst_sales 可以看自己分配的 sales 告警。
	salesToken := tokenFor("analyst_sales")
	status, _ = doJSON(t, http.MethodGet, ts.URL+"/api/v1/alerts/"+salesAlert.ID, salesToken, nil)
	if status != http.StatusNotFound && status != http.StatusOK {
		t.Fatalf("unexpected %d", status)
	}
	if status != http.StatusOK {
		t.Fatalf("analyst_sales should see sales alert, got %d", status)
	}

	// analyst 不能管理规则 / 时区（403）。
	status, _ = doJSON(t, http.MethodPost,
		ts.URL+"/api/v1/rules/"+models.RuleDownloadBurst+"/versions",
		engToken, map[string]any{"params": map[string]any{"threshold": 99}})
	if status != http.StatusForbidden {
		t.Fatalf("analyst rule write status = %d, want 403", status)
	}
	status, _ = doJSON(t, http.MethodPost,
		ts.URL+"/api/v1/employees/"+engEmp.ID+"/timezone",
		engToken, map[string]any{"timezone": "UTC"})
	if status != http.StatusForbidden {
		t.Fatalf("analyst tz write status = %d, want 403", status)
	}

	// admin 可见全部。
	adminToken := tokenFor("admin")
	status, body = doJSON(t, http.MethodGet, ts.URL+"/api/v1/alerts", adminToken, nil)
	if status != http.StatusOK {
		t.Fatalf("admin list = %d", status)
	}
	items, _ = body["items"].([]any)
	if len(items) < 2 {
		t.Fatalf("admin should see alerts across departments, got %d", len(items))
	}
	_ = engAlert
}

// TestAlertTransitionAndAudit 验证状态机与审计写入。
func TestAlertTransitionAndAudit(t *testing.T) {
	env := testsupport.New(t)
	ts, tokenFor := newHTTPServer(t, env)
	token := tokenFor("admin")
	emp := env.EmployeeByEmail(t, "frank.zhao@example.com")

	engine := env.Engine()
	if _, err := engine.ProcessEvents([]models.Event{
		testsupport.NewEvent("wf-usb", models.EventUSB, emp.ID,
			time.Now().UTC().Add(-2*time.Hour), map[string]any{"serial": "S"}),
	}); err != nil {
		t.Fatalf("process: %v", err)
	}
	var alert models.Alert
	if err := env.GDB.Where("employee_id = ? AND rule_key = ?", emp.ID, models.RuleFirstUSB).First(&alert).Error; err != nil {
		t.Fatalf("load alert: %v", err)
	}

	url := ts.URL + "/api/v1/alerts/" + alert.ID + "/transition"
	// 非法跳转：new -> new
	status, body := doJSON(t, http.MethodPost, url, token, map[string]any{"status": "new"})
	if status != http.StatusConflict {
		t.Fatalf("invalid transition status = %d body=%v", status, body)
	}
	// 合法：new -> investigating
	status, _ = doJSON(t, http.MethodPost, url, token, map[string]any{"status": "investigating", "note": "looking"})
	if status != http.StatusOK {
		t.Fatalf("investigating status = %d", status)
	}
	// investigating -> escalated -> resolved
	for _, st := range []string{"escalated", "resolved"} {
		status, _ = doJSON(t, http.MethodPost, url, token, map[string]any{"status": st})
		if status != http.StatusOK {
			t.Fatalf("transition to %s status = %d", st, status)
		}
	}
	var final models.Alert
	env.GDB.Where("id = ?", alert.ID).First(&final)
	if final.Status != models.AlertResolved || final.ResolvedAt == nil || final.AcknowledgedAt == nil {
		t.Fatalf("final state wrong: %+v", final)
	}
	var auditN int64
	env.GDB.Model(&models.AuditLog{}).
		Where("target_id = ? AND action = ?", alert.ID, "alert.transition").Count(&auditN)
	if auditN != 3 {
		t.Fatalf("audit rows = %d, want 3", auditN)
	}
}

// TestAdminRuleVersioning 管理员发布新规则版本，旧版本停用，修改有审计；
// 已有告警保留旧 rule_version。
func TestAdminRuleVersioning(t *testing.T) {
	env := testsupport.New(t)
	ts, tokenFor := newHTTPServer(t, env)
	admin := tokenFor("admin")
	emp := env.EmployeeByEmail(t, "frank.zhao@example.com")

	engine := env.Engine()
	base := time.Now().UTC().Add(-2 * time.Hour).Truncate(time.Hour)
	evs := []models.Event{}
	for i := 0; i < 51; i++ {
		evs = append(evs, testsupport.NewEvent(fmt.Sprintf("rv-%03d", i),
			models.EventFileDownload, emp.ID,
			base.Add(time.Duration(i)*5*time.Second), nil))
	}
	if _, err := engine.ProcessEvents(evs); err != nil {
		t.Fatalf("process: %v", err)
	}
	var before models.Alert
	if err := env.GDB.Where("rule_key = ?", models.RuleDownloadBurst).First(&before).Error; err != nil {
		t.Fatalf("alert: %v", err)
	}
	if before.RuleVersion != 1 {
		t.Fatalf("seed alert rule version = %d", before.RuleVersion)
	}

	status, body := doJSON(t, http.MethodPost,
		ts.URL+"/api/v1/rules/"+models.RuleDownloadBurst+"/versions",
		admin, map[string]any{"params": map[string]any{"window_minutes": 10, "threshold": 30}})
	if status != http.StatusCreated {
		t.Fatalf("create v2 status = %d body=%v", status, body)
	}

	var active []models.DetectionRule
	env.GDB.Where("rule_key = ? AND is_active = ?", models.RuleDownloadBurst, true).Find(&active)
	if len(active) != 1 || active[0].Version != 2 {
		t.Fatalf("active rules = %+v", active)
	}
	var old models.DetectionRule
	env.GDB.Where("rule_key = ? AND version = 1", models.RuleDownloadBurst).First(&old)
	if old.IsActive {
		t.Fatal("v1 should be deactivated")
	}

	// 旧告警版本不变。
	var kept models.Alert
	env.GDB.Where("id = ?", before.ID).First(&kept)
	if kept.RuleVersion != 1 {
		t.Fatalf("existing alert version changed to %d", kept.RuleVersion)
	}

	var auditN int64
	env.GDB.Model(&models.AuditLog{}).Where("action = ?", "rule.create_version").Count(&auditN)
	if auditN != 1 {
		t.Fatalf("rule audit rows = %d", auditN)
	}
}
