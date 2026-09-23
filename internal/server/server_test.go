package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"worksteal/internal/server"
)

func newTestServer(t *testing.T) (*httptest.Server, *server.Server) {
	t.Helper()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	srv := server.New(log)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(func() {
		ts.Close()
		srv.Close()
	})
	return ts, srv
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(ts.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}

func doReq(t *testing.T, ts *httptest.Server, method, path string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(method, ts.URL+path, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}

func decode(t *testing.T, resp *http.Response) (int, map[string]any) {
	t.Helper()
	data, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(data) > 0 {
		if err := json.Unmarshal(data, &m); err != nil {
			t.Fatalf("decode %q: %v", string(data), err)
		}
	}
	return resp.StatusCode, m
}

func TestHealthAndCreate(t *testing.T) {
	ts, _ := newTestServer(t)
	code, m := doReq(t, ts, "GET", "/healthz")
	if code != http.StatusOK || m["status"] != "ok" {
		t.Fatalf("health code=%d body=%v", code, m)
	}
	code, m = postJSON(t, ts, "/api/executors", map[string]any{"name": "w1", "workers": 2})
	if code != http.StatusCreated || m["name"] != "w1" {
		t.Fatalf("create code=%d body=%v", code, m)
	}
	// 重复创建冲突。
	code, _ = postJSON(t, ts, "/api/executors", map[string]any{"name": "w1", "workers": 2})
	if code != http.StatusConflict {
		t.Fatalf("dup create code=%d want 409", code)
	}
	// 列表包含。
	_, m = doReq(t, ts, "GET", "/api/executors")
	names, _ := m["executors"].([]any)
	if len(names) != 1 {
		t.Fatalf("executors=%v", m)
	}
}

func TestSubmitAndQuery(t *testing.T) {
	ts, _ := newTestServer(t)
	if _, m := postJSON(t, ts, "/api/executors", map[string]any{"name": "e", "workers": 2}); m == nil {
		t.Fatal("create failed")
	}
	// 提交 noop。
	code, m := postJSON(t, ts, "/api/executors/e/tasks",
		map[string]any{"type": "noop", "name": "hello"})
	if code != http.StatusAccepted {
		t.Fatalf("submit code=%d body=%v", code, m)
	}
	id, _ := m["id"].(string)
	if id == "" {
		t.Fatal("no task id")
	}
	// 轮询直到 completed。
	waitStatus(t, ts, "/api/executors/e/tasks/"+id, "completed", 3*time.Second)
	// 统计。
	_, sm := doReq(t, ts, "GET", "/api/executors/e")
	stats, _ := sm["stats"].(map[string]any)
	if stats["completed"].(float64) < 1 {
		t.Fatalf("stats=%v", stats)
	}
}

func TestTreeOverHTTP(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/api/executors", map[string]any{"name": "solo", "workers": 1})
	// 单 worker 深树：help-the-child 必须让它完成。
	code, m := postJSON(t, ts, "/api/executors/solo/tasks", map[string]any{
		"type":   "tree",
		"name":   "deep",
		"params": map[string]any{"depth": 8, "fanout": 2, "work_ms": 0},
	})
	if code != http.StatusAccepted {
		t.Fatalf("submit tree code=%d %v", code, m)
	}
	id := m["id"].(string)
	waitStatus(t, ts, "/api/executors/solo/tasks/"+id, "completed", 15*time.Second)
}

func TestCancelOverHTTP(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/api/executors", map[string]any{"name": "c", "workers": 1})
	// 提交一个长睡眠任务。
	_, m := postJSON(t, ts, "/api/executors/c/tasks", map[string]any{
		"type": "sleep", "params": map[string]any{"ms": 30000},
	})
	id := m["id"].(string)
	waitStatus(t, ts, "/api/executors/c/tasks/"+id, "running", 3*time.Second)
	code, cm := postJSON(t, ts, "/api/executors/c/tasks/"+id+"/cancel", map[string]any{})
	if code != http.StatusOK {
		t.Fatalf("cancel code=%d %v", code, cm)
	}
	waitStatus(t, ts, "/api/executors/c/tasks/"+id, "canceled", 3*time.Second)
}

func TestScheduleAndCancel(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/api/executors", map[string]any{"name": "sc", "workers": 1})
	code, m := postJSON(t, ts, "/api/executors/sc/schedules", map[string]any{
		"name":     "periodic",
		"every_ms": 50,
		"task":     map[string]any{"type": "noop"},
	})
	if code != http.StatusCreated || m["periodic"] != true {
		t.Fatalf("schedule code=%d %v", code, m)
	}
	sid := m["id"].(string)
	// 等几次触发后取消。
	time.Sleep(250 * time.Millisecond)
	code, _ = doReq(t, ts, "DELETE", "/api/executors/sc/schedules/"+sid)
	if code != http.StatusOK {
		t.Fatalf("cancel schedule code=%d", code)
	}
}

func TestDeleteExecutorGraceful(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/api/executors", map[string]any{"name": "g", "workers": 2})
	postJSON(t, ts, "/api/executors/g/tasks", map[string]any{"type": "noop"})
	code, m := doReq(t, ts, "DELETE", "/api/executors/g")
	if code != http.StatusOK || m["shutdown"] != "graceful" {
		t.Fatalf("delete code=%d %v", code, m)
	}
	// 删除后再提交应 404。
	code, _ = postJSON(t, ts, "/api/executors/g/tasks", map[string]any{"type": "noop"})
	if code != http.StatusNotFound {
		t.Fatalf("post after delete code=%d want 404", code)
	}
}

func TestEventStream(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/api/executors", map[string]any{"name": "ev", "workers": 1})
	// 打开 SSE。
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", ts.URL+"/api/executors/ev/events", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("sse status=%d", resp.StatusCode)
	}
	// 提交任务，应在流中看到相关事件。
	postJSON(t, ts, "/api/executors/ev/tasks", map[string]any{"type": "noop"})
	buf := make([]byte, 4096)
	deadline := time.Now().Add(3 * time.Second)
	var acc string
	for time.Now().Before(deadline) {
		n, _ := resp.Body.Read(buf)
		acc += string(buf[:n])
		if strings.Contains(acc, "task_completed") {
			return // 成功观察到结构化事件
		}
	}
	t.Fatalf("did not see completion event; stream tail=%q", acc)
}

func waitStatus(t *testing.T, ts *httptest.Server, path, want string, max time.Duration) {
	t.Helper()
	deadline := time.Now().Add(max)
	var last string
	for time.Now().Before(deadline) {
		_, m := doReq(t, ts, "GET", path)
		if s, ok := m["status"].(string); ok {
			last = s
			if s == want {
				return
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("status never %q (last=%q) at %s", want, last, path)
}
