package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"offline-scaler/internal/scaler"
)

func doPOST(t *testing.T, h http.Handler, body any) (*http.Response, []byte) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/replay", bytes.NewReader(raw))
	h.ServeHTTP(rec, req)
	resp := rec.Result()
	out, _ := io.ReadAll(resp.Body)
	return resp, out
}

func TestReplayEndpointOK(t *testing.T) {
	h := Handler()
	resp, body := doPOST(t, h, scaler.SpikeScenario().Request)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("状态码=%d，响应: %s", resp.StatusCode, body)
	}
	var wrapped struct {
		Result *scaler.ReplayResult `json:"result"`
	}
	if err := json.Unmarshal(body, &wrapped); err != nil {
		t.Fatalf("响应不是合法 JSON: %v；内容: %s", err, body)
	}
	if wrapped.Result == nil || len(wrapped.Result.Decisions) == 0 {
		t.Fatal("响应中应包含非空 decisions")
	}
	if wrapped.Result.Summary.ScaleUps == 0 {
		t.Fatal("尖峰场景经接口回放应产生扩容决策")
	}
}

// TestReplayEndpointBadJSON 非法 JSON → 400。
func TestReplayEndpointBadJSON(t *testing.T) {
	h := Handler()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/replay", strings.NewReader("{not json"))
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法 JSON 应返回 400，实际 %d", rec.Code)
	}
}

// TestReplayEndpointUnknownField 未知字段 → 400（DisallowUnknownFields）。
func TestReplayEndpointUnknownField(t *testing.T) {
	h := Handler()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/replay",
		strings.NewReader(`{"config":{},"samples":[],"bogus":1}`))
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("未知字段应返回 400，实际 %d", rec.Code)
	}
}

// TestReplayEndpointInvalidConfig 配置非法 → 400。
func TestReplayEndpointInvalidConfig(t *testing.T) {
	h := Handler()
	bad := scaler.RampScenario().Request
	bad.Config = scaler.Config{MinReplicas: 8, MaxReplicas: 3} // max < min
	resp, body := doPOST(t, h, bad)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("非法配置应返回 400，实际 %d: %s", resp.StatusCode, body)
	}
}

// TestReplayEndpointMethodNotAllowed GET → 405。
func TestReplayEndpointMethodNotAllowed(t *testing.T) {
	h := Handler()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/replay", nil)
	h.ServeHTTP(rec, req)
	// net/http 不自动设 405；本接口实现对 GET 返回 405。
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET 应返回 405，实际 %d", rec.Code)
	}
}

func TestHealthz(t *testing.T) {
	h := Handler()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "ok") {
		t.Fatalf("健康检查异常: %d %s", rec.Code, rec.Body.String())
	}
}

func TestRootAndNotFound(t *testing.T) {
	h := Handler()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/nope", nil)
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("未知路径应 404，实际 %d", rec.Code)
	}

	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodGet, "/", nil)
	h.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusOK {
		t.Fatalf("根路径应 200，实际 %d", rec2.Code)
	}
}
