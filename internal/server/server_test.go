package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"licensejudge/internal/cache"
	"licensejudge/internal/policy"
)

func newTestServer(t *testing.T) http.Handler {
	t.Helper()
	p := &policy.Policy{
		Name:     "test",
		Unlisted: policy.DecisionUnknown,
		Licenses: map[string]policy.LicenseRule{
			"MIT":                {Decision: policy.DecisionAllow},
			"Apache-2.0":         {Decision: policy.DecisionAllow},
			"GPL-3.0-only":       {Decision: policy.DecisionDeny},
			"LicenseRef-Pending": {Decision: policy.DecisionUnknown},
		},
		Exceptions: map[string]policy.ExceptionRule{
			"Classpath-exception-2.0": {Allow: true, AppliesTo: []string{"MIT"}},
		},
	}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	c, err := cache.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	srv, err := New(Config{
		Policy:  p,
		Cache:   c,
		WorkDir: t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = srv.Close() })
	return srv.Routes()
}

func postEval(t *testing.T, h http.Handler, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/evaluate", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestHealth(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz 状态码 = %d", rec.Code)
	}
}

func TestEvaluateAllow(t *testing.T) {
	h := newTestServer(t)
	code, out := postEval(t, h, `{"expression":"MIT OR GPL-3.0-only"}`)
	if code != http.StatusOK {
		t.Fatalf("状态码 = %d, body=%v", code, out)
	}
	result := out["result"].(map[string]any)
	if result["verdict"] != "allow" {
		t.Fatalf("verdict = %v", result["verdict"])
	}
	if result["selection"] != "MIT" {
		t.Fatalf("selection = %v, 期望 MIT", result["selection"])
	}
	alts := result["alternatives"].([]any)
	if len(alts) != 2 {
		t.Fatalf("alternatives 数量 = %d", len(alts))
	}
	if result["disclaimer"] == "" {
		t.Fatal("必须返回非法律声明")
	}
}

func TestEvaluateUnknown(t *testing.T) {
	h := newTestServer(t)
	code, out := postEval(t, h, `{"expression":"Never-Heard-Of-License"}`)
	if code != http.StatusOK {
		t.Fatalf("状态码 = %d", code)
	}
	result := out["result"].(map[string]any)
	if result["verdict"] != "unknown" {
		t.Fatalf("未知许可证必须为 unknown，实际 %v", result["verdict"])
	}
}

func TestEvaluateDenyWithReasons(t *testing.T) {
	h := newTestServer(t)
	code, out := postEval(t, h, `{"expression":"MIT AND GPL-3.0-only"}`)
	if code != http.StatusOK {
		t.Fatalf("状态码 = %d", code)
	}
	result := out["result"].(map[string]any)
	if result["verdict"] != "deny" {
		t.Fatalf("verdict = %v, 期望 deny", result["verdict"])
	}
	reasons := result["reasons"].([]any)
	if len(reasons) == 0 {
		t.Fatal("拒绝必须给出原因")
	}
}

func TestEvaluateParseError(t *testing.T) {
	h := newTestServer(t)
	code, out := postEval(t, h, `{"expression":"MIT AND"}`)
	if code != http.StatusBadRequest {
		t.Fatalf("解析错误应为 400，实际 %d", code)
	}
	if out["code"] != "parse_error" {
		t.Fatalf("错误码 = %v", out["code"])
	}
}

func TestEvaluateMissingExpression(t *testing.T) {
	h := newTestServer(t)
	code, _ := postEval(t, h, `{}`)
	if code != http.StatusBadRequest {
		t.Fatalf("空表达式应 400，实际 %d", code)
	}
}

func TestEvaluateInvalidJSON(t *testing.T) {
	h := newTestServer(t)
	code, _ := postEval(t, h, `{not json`)
	if code != http.StatusBadRequest {
		t.Fatalf("非法 JSON 应 400，实际 %d", code)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/evaluate", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET /v1/evaluate 应 405，实际 %d", rec.Code)
	}
}

func TestCacheHitSecondRequest(t *testing.T) {
	h := newTestServer(t)
	body := `{"expression":"Apache-2.0 AND MIT"}`
	_, out1 := postEval(t, h, body)
	if out1["cached"].(bool) {
		t.Fatal("第一次请求不应命中缓存")
	}
	_, out2 := postEval(t, h, body)
	if !out2["cached"].(bool) {
		t.Fatal("第二次请求应命中 AST 缓存")
	}
}

func TestWorkDirAndCacheSeparationEnforced(t *testing.T) {
	dir := t.TempDir()
	c, err := cache.New(filepath.Join(dir, "same"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = New(Config{
		Policy:  &policy.Policy{Name: "p", Licenses: map[string]policy.LicenseRule{}},
		Cache:   c,
		WorkDir: filepath.Join(dir, "same"),
	})
	if err == nil {
		t.Fatal("缓存目录与工作目录重合时必须拒绝启动")
	}
	if !strings.Contains(err.Error(), "分离") {
		t.Fatalf("错误信息应说明目录分离要求: %v", err)
	}
}

func TestWorkDirNestedUnderCacheRejected(t *testing.T) {
	root := t.TempDir()
	c, _ := cache.New(filepath.Join(root, "cache"))
	_, err := New(Config{
		Policy:  &policy.Policy{Name: "p", Licenses: map[string]policy.LicenseRule{}},
		Cache:   c,
		WorkDir: filepath.Join(root, "cache", "work"),
	})
	if err == nil {
		t.Fatal("工作目录位于缓存目录内时必须拒绝启动")
	}
}

func TestPolicyEndpoint(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/policies", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 %d", rec.Code)
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte(`"unlisted":"unknown"`)) {
		t.Fatalf("响应应包含 unlisted=unknown: %s", rec.Body.String())
	}
}
