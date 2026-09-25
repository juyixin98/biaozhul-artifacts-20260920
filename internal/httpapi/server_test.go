package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"traceassembly/trace"
)

var hT0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func hspan(id, parent, svc string, recvNS int64) trace.Span {
	return trace.Span{
		TraceID:       "tr-http",
		SpanID:        id,
		ParentSpanID:  parent,
		Service:       svc,
		Operation:     "op",
		Start:         hT0,
		DurationNanos: int64(10 * time.Millisecond),
		ReceiveNS:     recvNS,
	}
}

func postJSON(t *testing.T, h http.Handler, path string, body any) (int, map[string]any) {
	t.Helper()
	data, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(data))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var m map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &m)
	}
	return rec.Code, m
}

func getJSON(t *testing.T, h http.Handler, path string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var m map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &m)
	return rec.Code, m
}

func TestHTTPEndToEnd(t *testing.T) {
	asm := trace.New(trace.Config{TimeoutNS: 100, SkewToleranceNS: 1000}, nil)
	h := NewServer(asm).Mux

	// 健康检查。
	if code, m := getJSON(t, h, "/healthz"); code != 200 || m["status"] != "ok" {
		t.Fatalf("healthz: code=%d body=%v", code, m)
	}

	// 摄入缺根的两 span。
	code, m := postJSON(t, h, "/v1/spans", map[string]any{
		"spans": []trace.Span{
			hspan("g", "c", "edge", 20),
			hspan("c", "root", "api", 10),
		},
	})
	if code != 200 {
		t.Fatalf("ingest code=%d body=%v", code, m)
	}
	results := m["results"].([]any)
	if len(results) != 2 {
		t.Fatalf("want 2 results, got %v", results)
	}

	// 实时视图：存在但不完整、live=true。
	code, m = getJSON(t, h, "/v1/traces/tr-http")
	if code != 200 {
		t.Fatalf("get trace code=%d", code)
	}
	if m["live"] != true {
		t.Fatalf("expected live view: %v", m["live"])
	}

	// 列表。
	if code, m = getJSON(t, h, "/v1/traces"); code != 200 {
		t.Fatalf("list code=%d", code)
	}

	// 推进水位超时关闭，响应应携带一个 emitted revision。
	code, m = postJSON(t, h, "/v1/watermark", map[string]any{"watermark_ns": 200})
	if code != 200 {
		t.Fatalf("watermark code=%d body=%v", code, m)
	}
	emitted := m["emitted_revisions"].([]any)
	if len(emitted) != 1 {
		t.Fatalf("want 1 emitted, got %v", emitted)
	}
	rev0 := emitted[0].(map[string]any)
	if rev0["complete"] != false {
		t.Fatalf("rev1 should be incomplete: %v", rev0)
	}

	// 取历史 rev1。
	code, m = getJSON(t, h, "/v1/traces/tr-http/revisions/1")
	if code != 200 || m["revision"].(float64) != 1 {
		t.Fatalf("get rev1: code=%d body=%v", code, m)
	}
	if code, _ = getJSON(t, h, "/v1/traces/tr-http/revisions/9"); code != 404 {
		t.Fatalf("missing revision must be 404, got %d", code)
	}
	if code, _ = getJSON(t, h, "/v1/traces/nope"); code != 404 {
		t.Fatalf("missing trace must be 404, got %d", code)
	}

	// 迟到根 → 第二版。
	postJSON(t, h, "/v1/spans", map[string]any{"spans": []trace.Span{hspan("root", "", "gw", 300)}})
	_, m = postJSON(t, h, "/v1/watermark", map[string]any{"watermark_ns": 500})
	emitted = m["emitted_revisions"].([]any)
	rev2 := emitted[0].(map[string]any)
	if rev2["revision"].(float64) != 2 || rev2["revised_of"].(float64) != 1 {
		t.Fatalf("rev2 linkage wrong: %v", rev2)
	}
	if rev2["complete"] != true {
		t.Fatalf("rev2 complete: reasons=%v", rev2["reasons"])
	}

	// 校验错误返回 4xx 且状态不变。
	code, m = postJSON(t, h, "/v1/spans", map[string]any{"spans": []trace.Span{{TraceID: "x"}}})
	if code != http.StatusBadRequest {
		t.Fatalf("validation should be 400, got %d", code)
	}
	if !strings.Contains(m["error"].(string), "span_id") {
		t.Fatalf("error should mention span_id: %v", m["error"])
	}
}
