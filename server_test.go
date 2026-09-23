package agingqueue

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestServer(t *testing.T, cfg Config) (*Server, *Scheduler, *FakeClock) {
	t.Helper()
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	if cfg.Clock == nil {
		cfg.Clock = clk
	}
	cfg.SynchronousExec = true
	if cfg.MaxConcurrency == 0 {
		cfg.MaxConcurrency = 2
	}
	s := NewScheduler(cfg)
	RegisterBuiltinExecutors(s, clk)
	s.Start()
	t.Cleanup(func() { s.Close() })
	return NewServer(s), s, clk
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
	}
	return rec.Code, out
}

func TestHTTP_SubmitGetListCancel(t *testing.T) {
	srv, _, _ := newTestServer(t, Config{AgingStep: time.Second})
	h := srv.Handler()

	// 提交一个 echo 作业（同步执行下立即成功）。
	code, body := doJSON(t, h, "POST", "/jobs", map[string]any{
		"id": "j1", "type": "echo", "priority": 3, "payload": "hello",
	})
	if code != http.StatusCreated {
		t.Fatalf("submit status=%d body=%v", code, body)
	}
	if body["job"].(map[string]any)["id"] != "j1" {
		t.Fatalf("unexpected submit body: %v", body)
	}
	// 提交响应反映入队时刻；同步执行下重新读取应已成功。
	code, body = doJSON(t, h, "GET", "/jobs/j1", nil)
	if code != http.StatusOK {
		t.Fatalf("get status=%d", code)
	}
	job := body["job"].(map[string]any)
	if job["state"] != "succeeded" {
		t.Fatalf("unexpected job state: %v", job)
	}
	if job["result"] != "hello" {
		t.Fatalf("result=%v want hello", job["result"])
	}

	// 再提交一个会排队的高优先级 sleep（未跃迁时钟，保持排队需要占槽，
	// 这里仅验证 GET/LIST 返回结构，用 echo 即可）。
	_, body = doJSON(t, h, "POST", "/jobs", map[string]any{"type": "echo", "priority": 9})
	if body["outcome"] != "submitted" {
		t.Fatalf("outcome=%v", body["outcome"])
	}

	code, body = doJSON(t, h, "GET", "/jobs/j1", nil)
	if code != http.StatusOK || body["job"] == nil {
		t.Fatalf("get status=%d body=%v", code, body)
	}

	code, body = doJSON(t, h, "GET", "/jobs", nil)
	jobs := body["jobs"].([]any)
	if code != http.StatusOK || len(jobs) < 2 {
		t.Fatalf("list status=%d jobs=%v", code, jobs)
	}
}

func TestHTTP_Validation(t *testing.T) {
	srv, _, _ := newTestServer(t, Config{})
	h := srv.Handler()

	// 未知类型 -> 400
	code, body := doJSON(t, h, "POST", "/jobs", map[string]any{"type": "nope", "priority": 1})
	if code != http.StatusBadRequest {
		t.Fatalf("unknown type status=%d", code)
	}
	if !strings.Contains(body["error"].(string), "no executor") {
		t.Fatalf("error=%v", body["error"])
	}

	// 优先级越界 -> 400
	code, _ = doJSON(t, h, "POST", "/jobs", map[string]any{"type": "echo", "priority": 99})
	if code != http.StatusBadRequest {
		t.Fatalf("bad priority status=%d", code)
	}

	// 坏 JSON -> 400
	req := httptest.NewRequest("POST", "/jobs", bytes.NewReader([]byte("{not json")))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("bad json status=%d", rec.Code)
	}

	// 404
	code, _ = doJSON(t, h, "GET", "/jobs/missing", nil)
	if code != http.StatusNotFound {
		t.Fatalf("missing job status=%d", code)
	}
}

func TestHTTP_DuplicateIDConflict(t *testing.T) {
	srv, _, _ := newTestServer(t, Config{})
	h := srv.Handler()
	doJSON(t, h, "POST", "/jobs", map[string]any{"id": "dup", "type": "echo", "priority": 1})
	code, _ := doJSON(t, h, "POST", "/jobs", map[string]any{"id": "dup", "type": "echo", "priority": 1})
	if code != http.StatusConflict {
		t.Fatalf("duplicate status=%d, want 409", code)
	}
}

func TestHTTP_CancelQueuedAndRepeated(t *testing.T) {
	// 用异步 + gate 占槽，制造一个确定排队的作业。
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	s := NewScheduler(Config{Clock: clk, MaxConcurrency: 1, SynchronousExec: false})
	gate := NewGateExecutor()
	s.RegisterExecutor("gate", gate)
	s.Start()
	defer s.Close()
	h := NewServer(s).Handler()

	doJSON(t, h, "POST", "/jobs", map[string]any{"id": "hold", "type": "gate", "priority": 9})
	if !waitForState(s, "hold", StateRunning) {
		t.Fatal("hold not running")
	}
	doJSON(t, h, "POST", "/jobs", map[string]any{"id": "v", "type": "gate", "priority": 1})
	if !waitForState(s, "v", StateQueued) {
		t.Fatal("v not queued")
	}

	code, body := doJSON(t, h, "POST", "/jobs/v/cancel", nil)
	if code != http.StatusOK || body["outcome"] != "canceled" {
		t.Fatalf("first cancel code=%d body=%v", code, body)
	}
	// 重复取消终态作业 -> 409
	code, _ = doJSON(t, h, "POST", "/jobs/v/cancel", nil)
	if code != http.StatusConflict {
		t.Fatalf("repeat cancel status=%d want 409", code)
	}

	gate.Release()
}

func TestHTTP_MetricsAndHealth(t *testing.T) {
	srv, _, _ := newTestServer(t, Config{})
	h := srv.Handler()

	code, _ := doJSON(t, h, "GET", "/healthz", nil)
	if code != http.StatusOK {
		t.Fatalf("health=%d", code)
	}
	doJSON(t, h, "POST", "/jobs", map[string]any{"type": "echo", "priority": 1})
	code, body := doJSON(t, h, "GET", "/metrics", nil)
	if code != http.StatusOK || body["total"].(float64) < 1 {
		t.Fatalf("metrics code=%d body=%v", code, body)
	}
	if body["succeeded"].(float64) < 1 {
		t.Fatalf("expected a succeeded job, got %v", body)
	}
}

func TestHTTP_EventsEndpoint(t *testing.T) {
	srv, _, _ := newTestServer(t, Config{})
	h := srv.Handler()
	doJSON(t, h, "POST", "/jobs", map[string]any{"type": "echo", "priority": 2})

	code, body := doJSON(t, h, "GET", "/events?limit=50", nil)
	if code != http.StatusOK {
		t.Fatalf("events=%d", code)
	}
	events := body["events"].([]any)
	if len(events) == 0 {
		t.Fatal("expected events")
	}
	// 至少应包含 submitted 与 succeeded。
	var kinds []string
	for _, e := range events {
		kinds = append(kinds, e.(map[string]any)["type"].(string))
	}
	joined := strings.Join(kinds, ",")
	if !strings.Contains(joined, "submitted") || !strings.Contains(joined, "succeeded") {
		t.Fatalf("event kinds=%s", joined)
	}
}

// 验证内置 sleep 执行器在假时钟下可被跃迁完成。
func TestHTTP_SleepJobAdvancesWithFakeClock(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	s := NewScheduler(Config{Clock: clk, MaxConcurrency: 1, SynchronousExec: false})
	RegisterBuiltinExecutors(s, clk)
	s.Start()
	defer s.Close()
	h := NewServer(s).Handler()

	code, body := doJSON(t, h, "POST", "/jobs",
		map[string]any{"type": "sleep", "priority": 5, "payload": map[string]any{"ms": 100}})
	if code != http.StatusCreated {
		t.Fatalf("submit=%d body=%v", code, body)
	}
	id := body["job"].(map[string]any)["id"].(string)
	if !waitForState(s, id, StateRunning) {
		t.Fatal("sleep job not running")
	}
	// 执行器可能在 running 之后才向假时钟注册定时器：小步跃迁直到完成，
	// 每一步都会触发已注册的定时器。
	deadline := time.Now().Add(2 * time.Second)
	for {
		clk.Advance(20 * time.Millisecond)
		v, _ := s.Get(id)
		if v.State == StateSucceeded {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("sleep job state=%s, want succeeded after clock advance", v.State)
		}
	}
}
