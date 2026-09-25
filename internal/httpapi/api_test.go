package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"contractcheck/internal/clock"
	"contractcheck/internal/compat"
	"contractcheck/internal/registry"
)

func newTestServer(t *testing.T) (*httptest.Server, *registry.Store) {
	t.Helper()
	store := registry.New()
	fake := clock.NewFake(time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC))
	srv := NewServer(store, fake)
	ts := httptest.NewServer(srv.Mux)
	t.Cleanup(ts.Close)
	return ts, store
}

func postJSON(t *testing.T, url string, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(url, "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("POST %s 失败: %v", url, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	return resp.StatusCode, out
}

func TestHealthz(t *testing.T) {
	ts, _ := newTestServer(t)
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("期望 200，得到 %d", resp.StatusCode)
	}
}

// 端到端：内联 schema 检查，请求方向新增必填字段必须报不兼容并带示例值。
func TestCheckInlineIncompatible(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{
		"direction": "request",
		"old": {"schema": {"type":"object","required":["name"],"properties":{"name":{"type":"string"},"email":{"type":"string"}}}},
		"new": {"schema": {"type":"object","required":["name","email"],"properties":{"name":{"type":"string"},"email":{"type":"string"}}}}
	}`
	code, out := postJSON(t, ts.URL+"/v1/compat/check", body)
	if code != http.StatusOK {
		t.Fatalf("期望 200，得到 %d: %v", code, out)
	}
	if out["status"] != compat.StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %v", out["status"])
	}
	findings, ok := out["findings"].([]any)
	if !ok || len(findings) == 0 {
		t.Fatalf("应有 findings: %v", out)
	}
	f0 := findings[0].(map[string]any)
	if f0["kind"] != "required" || f0["example"] == nil {
		t.Fatalf("finding 缺少 kind/example: %v", f0)
	}
	if out["checkedAt"] != "2026-09-25T12:00:00Z" {
		t.Fatalf("checkedAt 应来自可控时钟，得到 %v", out["checkedAt"])
	}
}

// 端到端：不支持关键字必须返回 unknown 而非通过。
func TestCheckUnknownKeyword(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{
		"direction": "response",
		"old": {"schema": {"type":"string"}},
		"new": {"schema": {"type":"string","pattern":"^[a-z]+$"}}
	}`
	code, out := postJSON(t, ts.URL+"/v1/compat/check", body)
	if code != http.StatusOK {
		t.Fatalf("期望 200，得到 %d", code)
	}
	if out["status"] != compat.StatusUnknown {
		t.Fatalf("期望 unknown，得到 %v", out["status"])
	}
	unknowns, ok := out["unknownKeywords"].([]any)
	if !ok || len(unknowns) != 1 {
		t.Fatalf("应列出未知关键字: %v", out)
	}
}

// 端到端：通过仓库引用检查已注册契约。
func TestCheckViaRegistry(t *testing.T) {
	ts, store := newTestServer(t)
	store.Put(registry.Contract{
		Name: "user-api", Version: "v1",
		Schema: map[string]any{"type": "object", "required": []any{"id"},
			"properties": map[string]any{"id": map[string]any{"type": "integer"}}},
	})
	store.Put(registry.Contract{
		Name: "user-api", Version: "v2",
		Schema: map[string]any{"type": "object", "required": []any{"id", "name"},
			"properties": map[string]any{"id": map[string]any{"type": "integer"},
				"name": map[string]any{"type": "string"}}},
	})
	body := `{
		"direction": "request",
		"old": {"name": "user-api", "version": "v1"},
		"new": {"name": "user-api", "version": "v2"}
	}`
	code, out := postJSON(t, ts.URL+"/v1/compat/check", body)
	if code != http.StatusOK {
		t.Fatalf("期望 200，得到 %d", code)
	}
	if out["status"] != compat.StatusIncompatible {
		t.Fatalf("v1->v2 请求方向应不兼容，得到 %v", out["status"])
	}
}

func TestCheckBadDirection(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{"direction":"sideways","old":{"schema":{}},"new":{"schema":{}}}`
	code, out := postJSON(t, ts.URL+"/v1/compat/check", body)
	if code != http.StatusBadRequest {
		t.Fatalf("非法方向应 400，得到 %d: %v", code, out)
	}
}

func TestCheckMissingRef(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{"direction":"request","old":{},"new":{"schema":{}}}`
	code, _ := postJSON(t, ts.URL+"/v1/compat/check", body)
	if code != http.StatusBadRequest {
		t.Fatalf("缺少 schema/引用应 400，得到 %d", code)
	}
}

func TestPutAndListContracts(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{"name":"a","version":"v1","schema":{"type":"string"}}`
	code, _ := postJSON(t, ts.URL+"/v1/contracts", body)
	if code != http.StatusCreated {
		t.Fatalf("期望 201，得到 %d", code)
	}
	resp, err := http.Get(ts.URL + "/v1/contracts")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	contracts := out["contracts"].([]any)
	if len(contracts) != 1 {
		t.Fatalf("应有 1 份契约，得到 %v", out)
	}
}

// 端到端：probe 端点注入 flaky 故障，客户端重试后成功。
func TestProbeFlaky(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{
		"fault": {"kind": "flaky", "flakyTimes": 1},
		"client": {"maxAttempts": 3, "initialBackoffMs": 10, "maxBackoffMs": 100, "useFakeTime": true}
	}`
	code, out := postJSON(t, ts.URL+"/v1/probe", body)
	if code != http.StatusOK {
		t.Fatalf("期望 200，得到 %d", code)
	}
	if out["success"] != true {
		t.Fatalf("flaky 后应重试成功: %v", out)
	}
	attempts := out["attempts"].([]any)
	if len(attempts) != 2 {
		t.Fatalf("应尝试 2 次，得到 %v", attempts)
	}
}

// 端到端：probe 端点注入持续 500，尝试耗尽后失败。
func TestProbePersistentFailure(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{
		"fault": {"kind": "status", "statusCode": 500},
		"client": {"maxAttempts": 2, "initialBackoffMs": 5, "maxBackoffMs": 50, "useFakeTime": true}
	}`
	code, out := postJSON(t, ts.URL+"/v1/probe", body)
	if code != http.StatusOK {
		t.Fatalf("期望 200，得到 %d", code)
	}
	if out["success"] != false {
		t.Fatalf("持续 500 应失败: %v", out)
	}
	if out["finalStatus"].(float64) != 500 {
		t.Fatalf("最终状态码应为 500: %v", out)
	}
}
