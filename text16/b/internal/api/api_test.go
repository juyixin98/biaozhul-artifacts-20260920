package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/example/forensiccore/internal/config"
	"github.com/example/forensiccore/internal/service"
	"github.com/example/forensiccore/internal/store"
)

type apiEnv struct {
	svc    *service.Service
	router http.Handler
	root   string
}

func newAPIEnv(t *testing.T) *apiEnv {
	t.Helper()
	dir := t.TempDir()
	dsn := filepath.Join(t.TempDir(), "t.db")
	db, err := store.Open("sqlite", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB, _ := db.DB(); _ = sqlDB.Close() })

	cfg := &config.Config{
		DBDriver: "sqlite", DSN: dsn,
		ChunkSize: 4096, TokenTTLHours: 1, JWTSecret: []byte("secret"),
		Roots: map[string]string{"evidence": dir},
		Users: map[string]config.User{
			"inv": {Password: "p", Role: config.RoleInvestigator},
			"ana": {Password: "p", Role: config.RoleAnalyst},
		},
	}
	svc, err := service.New(db, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(svc.Close)
	return &apiEnv{svc: svc, router: Router(svc), root: dir}
}

func (e *apiEnv) token(t *testing.T, user string) string {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"username": user, "password": "p"})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/token", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("token status = %d body=%s", w.Code, w.Body.String())
	}
	var resp struct {
		Token string `json:"token"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp.Token
}

func (e *apiEnv) do(t *testing.T, method, path, token string, payload any) *httptest.ResponseRecorder {
	t.Helper()
	var rdr *bytes.Reader
	if payload != nil {
		b, _ := json.Marshal(payload)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	return w
}

func (e *apiEnv) writeImage(t *testing.T, rel string, size int) {
	t.Helper()
	full := filepath.Join(e.root, rel)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatal(err)
	}
	// 简单确定性数据。
	if err := os.WriteFile(full, bytes.Repeat([]byte{0xAB}, size), 0o644); err != nil {
		t.Fatal(err)
	}
}

// 分析师只能查询与备注：登记、发起复核、移交都应 403。
func TestRBAC_AnalystRestrictions(t *testing.T) {
	env := newAPIEnv(t)
	invTok := env.token(t, "inv")
	anaTok := env.token(t, "ana")

	// 调查员建案件。
	w := env.do(t, http.MethodPost, "/api/v1/cases", invTok, map[string]string{
		"case_number": "RBAC-1", "title": "x", "custodian": "c",
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("create case = %d %s", w.Code, w.Body.String())
	}

	// 分析师建案件被拒。
	if w := env.do(t, http.MethodPost, "/api/v1/cases", anaTok, map[string]string{
		"case_number": "RBAC-X", "title": "x", "custodian": "c",
	}); w.Code != http.StatusForbidden {
		t.Fatalf("analyst create case = %d, want 403", w.Code)
	}

	// 分析师可以查询案件列表。
	if w := env.do(t, http.MethodGet, "/api/v1/cases", anaTok, nil); w.Code != http.StatusOK {
		t.Fatalf("analyst list cases = %d, want 200", w.Code)
	}

	// 准备一个已登记证据。
	env.writeImage(t, "d.raw", 4096*2)
	w = env.do(t, http.MethodPost, "/api/v1/cases/1/evidences", invTok, map[string]string{
		"root_name": "evidence", "rel_path": "d.raw", "name": "d.raw",
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("register = %d %s", w.Code, w.Body.String())
	}

	// 分析师登记证据被拒。
	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/evidences", anaTok, map[string]string{
		"root_name": "evidence", "rel_path": "d.raw",
	}); w.Code != http.StatusForbidden {
		t.Fatalf("analyst register = %d, want 403", w.Code)
	}

	// 分析师发起复核被拒。
	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/evidences/1/verify", anaTok, nil); w.Code != http.StatusForbidden {
		t.Fatalf("analyst verify = %d, want 403", w.Code)
	}

	// 分析师移交被拒。
	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/evidences/1/transfer", anaTok, map[string]string{
		"to": "bob",
	}); w.Code != http.StatusForbidden {
		t.Fatalf("analyst transfer = %d, want 403", w.Code)
	}

	// 分析师备注允许。
	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/notes", anaTok, map[string]string{
		"text": "analyst note",
	}); w.Code != http.StatusCreated {
		t.Fatalf("analyst note = %d %s, want 201", w.Code, w.Body.String())
	}

	// 分析师可查询证据链与校验。
	if w := env.do(t, http.MethodGet, "/api/v1/cases/1/chain", anaTok, nil); w.Code != http.StatusOK {
		t.Fatalf("analyst chain = %d", w.Code)
	}
	if w := env.do(t, http.MethodGet, "/api/v1/cases/1/chain/verify", anaTok, nil); w.Code != http.StatusOK {
		t.Fatalf("analyst verify chain = %d", w.Code)
	}
}

// 无令牌访问受保护接口应 401；错误密码也应 401。
func TestAuth_MissingAndBadCredentials(t *testing.T) {
	env := newAPIEnv(t)
	if w := env.do(t, http.MethodGet, "/api/v1/cases", "", nil); w.Code != http.StatusUnauthorized {
		t.Fatalf("no token = %d, want 401", w.Code)
	}
	body, _ := json.Marshal(map[string]string{"username": "inv", "password": "wrong"})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/token", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	env.router.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("bad password = %d, want 401", w.Code)
	}
}

// 调查员完整走通登记 → 复核 → 移交 → 报告导出。
func TestEndToEnd_InvestigatorFlowAndReport(t *testing.T) {
	env := newAPIEnv(t)
	tok := env.token(t, "inv")

	if w := env.do(t, http.MethodPost, "/api/v1/cases", tok, map[string]string{
		"case_number": "E2E-1", "title": "flow", "custodian": "alice",
	}); w.Code != http.StatusCreated {
		t.Fatal(w.Body.String())
	}
	env.writeImage(t, "e2e.raw", 4096*3)
	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/evidences", tok, map[string]string{
		"root_name": "evidence", "rel_path": "e2e.raw",
	}); w.Code != http.StatusCreated {
		t.Fatalf("register: %s", w.Body.String())
	}
	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/evidences/1/verify", tok, nil); w.Code != http.StatusAccepted {
		t.Fatalf("verify: %s", w.Body.String())
	}

	// 复核为后台持久化作业，轮询直到完成。
	deadline := time.Now().Add(5 * time.Second)
	var verified bool
	for time.Now().Before(deadline) {
		jw := env.do(t, http.MethodGet, "/api/v1/cases/1/jobs", tok, nil)
		var jobs struct {
			Jobs []struct {
				Status string `json:"status"`
			} `json:"jobs"`
		}
		_ = json.Unmarshal(jw.Body.Bytes(), &jobs)
		for _, j := range jobs.Jobs {
			if j.Status == "verified" {
				verified = true
			}
		}
		if verified {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !verified {
		t.Fatal("verify job did not reach verified")
	}

	if w := env.do(t, http.MethodPost, "/api/v1/cases/1/evidences/1/transfer", tok, map[string]string{
		"to": "bob", "reason": "handover",
	}); w.Code != http.StatusCreated {
		t.Fatalf("transfer: %s", w.Body.String())
	}

	// JSON 报告应包含基线、复核与链，以及能力边界声明。
	w := env.do(t, http.MethodGet, "/api/v1/cases/1/report", tok, nil)
	if w.Code != http.StatusOK {
		t.Fatalf("report: %s", w.Body.String())
	}
	body := w.Body.String()
	for _, want := range []string{"sha256", `"chain"`, `"evidences"`, `"jobs"`, "可信时间"} {
		if !bytes.Contains([]byte(body), []byte(want)) {
			t.Fatalf("report missing %q", want)
		}
	}

	// Markdown 报告。
	w = env.do(t, http.MethodGet, "/api/v1/cases/1/report?format=markdown", tok, nil)
	if w.Code != http.StatusOK {
		t.Fatalf("markdown report = %d", w.Code)
	}
}
