package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"criticalpath/internal/analyzer"
	"criticalpath/internal/store"
)

func newTestServer(t *testing.T) *Server {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return NewServer(st)
}

func do(t *testing.T, h http.Handler, method, path, body string) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != "" {
		rdr = bytes.NewReader([]byte(body))
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("响应不是 JSON: %v\n%s", err, rec.Body.String())
		}
	}
	return rec.Code, out
}

func TestHealthAndNotFound(t *testing.T) {
	s := newTestServer(t)
	if code, body := do(t, s.Mux, "GET", "/healthz", ""); code != 200 || body["status"] != "ok" {
		t.Fatalf("health = %d %v", code, body)
	}
	if code, _ := do(t, s.Mux, "GET", "/v1/traces/nope/critical-path", ""); code != http.StatusNotFound {
		t.Fatalf("不存在的 trace 应 404，实际 %d", code)
	}
}

func TestSampleAndCriticalPath(t *testing.T) {
	s := newTestServer(t)
	code, body := do(t, s.Mux, "POST", "/v1/sample/demo?trace_id=t1", "")
	if code != http.StatusOK {
		t.Fatalf("建样例 = %d: %v", code, body)
	}
	code, body = do(t, s.Mux, "GET", "/v1/traces/t1/critical-path", "")
	if code != http.StatusOK {
		t.Fatalf("查询关键路径 = %d: %v", code, body)
	}
	if dur, _ := body["critical_path_duration_us"].(float64); dur != 300 {
		t.Errorf("关键路径时长 = %v，期望 300", body["critical_path_duration_us"])
	}
	cp, _ := body["critical_path"].([]any)
	if len(cp) != 4 {
		t.Errorf("关键路径项数 = %d，期望 4", len(cp))
	}
}

func TestIngestValidation(t *testing.T) {
	s := newTestServer(t)
	// 非 JSON
	if code, _ := do(t, s.Mux, "POST", "/v1/traces/t/spans", "{bad"); code != http.StatusBadRequest {
		t.Errorf("坏 JSON 应 400，实际 %d", code)
	}
	// 空 spans
	if code, _ := do(t, s.Mux, "POST", "/v1/traces/t/spans", `{"spans":[]}`); code != http.StatusBadRequest {
		t.Errorf("空 spans 应 400，实际 %d", code)
	}
	// trace_id 不一致
	body := `{"spans":[{"span_id":"a","name":"a","trace_id":"other","start_us":0,"end_us":1}]}`
	if code, _ := do(t, s.Mux, "POST", "/v1/traces/t/spans", body); code != http.StatusBadRequest {
		t.Errorf("trace_id 不一致应 400，实际 %d", code)
	}
}

func TestIngestCycleReturns422(t *testing.T) {
	s := newTestServer(t)
	var lines []string
	for _, sp := range analyzer.CycleSpans("ct") {
		b, _ := json.Marshal(sp)
		lines = append(lines, string(b))
	}
	body := `{"spans":[` + strings.Join(lines, ",") + `]}`
	code, resp := do(t, s.Mux, "POST", "/v1/traces/ct/spans", body)
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("循环输入应 422，实际 %d: %v", code, resp)
	}
	// 数据仍然落库，可查（同样返回 422）。
	code, _ = do(t, s.Mux, "GET", "/v1/traces/ct/critical-path", "")
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("查询循环 trace 应 422，实际 %d", code)
	}
}

func TestUnknownSampleKind(t *testing.T) {
	s := newTestServer(t)
	if code, _ := do(t, s.Mux, "POST", "/v1/sample/bogus", ""); code != http.StatusBadRequest {
		t.Fatalf("未知 kind 应 400，实际 %d", code)
	}
}
