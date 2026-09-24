package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/example/drf-scheduler/internal/scheduler"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	sch, err := scheduler.New(scheduler.Resources{CPU: 10000, Mem: 10000})
	if err != nil {
		t.Fatal(err)
	}
	return httptest.NewServer(New(sch))
}

func do(t *testing.T, method, url, body string) (int, map[string]any) {
	t.Helper()
	var r io.Reader
	if body != "" {
		r = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, url, r)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &m); err != nil {
			t.Fatalf("response not json: %v: %s", err, raw)
		}
	}
	return resp.StatusCode, m
}

func TestHTTP_HealthAndState(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	if code, m := do(t, "GET", srv.URL+"/healthz", ""); code != http.StatusOK || m["status"] != "ok" {
		t.Fatalf("health: code=%d body=%v", code, m)
	}
	code, m := do(t, "GET", srv.URL+"/state", "")
	if code != http.StatusOK {
		t.Fatalf("state: %d", code)
	}
	cap := m["capacity"].(map[string]any)
	if cap["cpu"].(float64) != 10000 || cap["mem"].(float64) != 10000 {
		t.Fatalf("bad capacity: %v", cap)
	}
}

func TestHTTP_FullLifecycle(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	// 创建两个等权租户
	for _, id := range []string{"alpha", "beta"} {
		if code, m := do(t, "POST", srv.URL+"/tenants", `{"id":"`+id+`"}`); code != http.StatusCreated {
			t.Fatalf("create %s: code=%d body=%v", id, code, m)
		}
	}
	// 重复创建 -> 409
	if code, _ := do(t, "POST", srv.URL+"/tenants", `{"id":"alpha"}`); code != http.StatusConflict {
		t.Fatalf("duplicate tenant: want 409, got %d", code)
	}

	// beta 内存密集任务先占 6000 内存
	code, m := do(t, "POST", srv.URL+"/tasks", `{"id":"b1","tenant":"beta","cpu":1000,"mem":6000}`)
	if code != http.StatusCreated || m["status"] != "running" {
		t.Fatalf("b1: code=%d body=%v", code, m)
	}
	// alpha CPU 密集任务
	code, m = do(t, "POST", srv.URL+"/tasks", `{"id":"a1","tenant":"alpha","cpu":6000,"mem":1000}`)
	if code != http.StatusCreated || m["status"] != "running" {
		t.Fatalf("a1: code=%d body=%v", code, m)
	}
	// 第二个 alpha 任务无法放置 -> 201 且 status=queued（排队保留）
	code, m = do(t, "POST", srv.URL+"/tasks", `{"id":"a2","tenant":"alpha","cpu":6000,"mem":1000}`)
	if code != http.StatusCreated || m["status"] != "queued" {
		t.Fatalf("a2: code=%d body=%v", code, m)
	}

	// GET 单个排队任务，应带阻塞原因
	code, m = do(t, "GET", srv.URL+"/tasks/a2", "")
	if code != http.StatusOK {
		t.Fatalf("get a2: %d", code)
	}
	task := m["task"].(map[string]any)
	if task["blocked_reason"] != "insufficient_cluster_cpu" {
		t.Fatalf("a2 reason = %v", task["blocked_reason"])
	}

	// 释放 b1：a2 需要的是 CPU，b1 只占 1000 CPU，仍不够 6000 -> a2 继续排队
	code, m = do(t, "DELETE", srv.URL+"/tasks/b1", "")
	if code != http.StatusOK {
		t.Fatalf("release b1: %d", code)
	}
	state := m["state"].(map[string]any)
	if state["num_queued"].(float64) != 1 {
		t.Fatalf("a2 should still be queued after b1 release, state=%v", state)
	}
	// 释放 a1 -> a2 立即运行（释放后重调度）
	code, m = do(t, "DELETE", srv.URL+"/tasks/a1", "")
	if code != http.StatusOK {
		t.Fatalf("release a1: %d", code)
	}
	state = m["state"].(map[string]any)
	if state["num_queued"].(float64) != 0 || state["num_running"].(float64) != 1 {
		t.Fatalf("a2 should be rescheduled, state=%v", state)
	}
	// 资源守恒
	used := state["used"].(map[string]any)
	free := state["free"].(map[string]any)
	if used["cpu"].(float64)+free["cpu"].(float64) != 10000 ||
		used["mem"].(float64)+free["mem"].(float64) != 10000 {
		t.Fatalf("resource conservation broken: used=%v free=%v", used, free)
	}

	// 404
	if code, _ := do(t, "GET", srv.URL+"/tasks/nope", ""); code != http.StatusNotFound {
		t.Fatalf("missing task: want 404, got %d", code)
	}
	if code, _ := do(t, "DELETE", srv.URL+"/tenants/alpha", ""); code != http.StatusConflict {
		t.Fatalf("delete non-empty tenant: want 409, got %d", code)
	}
}

func TestHTTP_FractionWeightAndQuota(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	// 权重支持分数字符串与数字两种写法
	if code, m := do(t, "POST", srv.URL+"/tenants", `{"id":"a","weight":"1/3"}`); code != http.StatusCreated {
		t.Fatalf("fraction weight: code=%d body=%v", code, m)
	}
	if code, m := do(t, "POST", srv.URL+"/tenants", `{"id":"b","weight":2.5}`); code != http.StatusCreated {
		t.Fatalf("number weight: code=%d body=%v", code, m)
	}
	if code, m := do(t, "POST", srv.URL+"/tenants", `{"id":"c","weight":"abc"}`); code != http.StatusBadRequest {
		t.Fatalf("bad weight: want 400, got %d body=%v", code, m)
	}
	// 带配额的租户
	if code, m := do(t, "POST", srv.URL+"/tenants",
		`{"id":"q","weight":1,"quota":{"cpu":4000,"mem":4000}}`); code != http.StatusCreated {
		t.Fatalf("quota tenant: code=%d body=%v", code, m)
	}
	// 配额超过容量 -> 400
	if code, _ := do(t, "POST", srv.URL+"/tenants", `{"id":"big","quota":{"cpu":99999,"mem":1}}`); code != http.StatusBadRequest {
		t.Fatalf("oversized quota: want 400, got %d", code)
	}

	// 校验返回的权重是精确分数
	_, m := do(t, "GET", srv.URL+"/tenants/a", "")
	if m["weight"] != "1/3" {
		t.Fatalf("weight echo = %v, want 1/3", m["weight"])
	}
}

func TestHTTP_BadRequests(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	// 未知字段
	if code, _ := do(t, "POST", srv.URL+"/tenants", `{"id":"x","bogus":1}`); code != http.StatusBadRequest {
		t.Fatalf("unknown field: want 400, got %d", code)
	}
	// 空体
	if code, _ := do(t, "POST", srv.URL+"/tenants", ``); code != http.StatusBadRequest {
		t.Fatalf("empty body: want 400, got %d", code)
	}
	// 尾随数据
	if code, _ := do(t, "POST", srv.URL+"/tenants", `{"id":"x"} {}`); code != http.StatusBadRequest {
		t.Fatalf("trailing data: want 400, got %d", code)
	}
	// 任务提交给不存在的租户
	if code, _ := do(t, "POST", srv.URL+"/tasks", `{"id":"t","tenant":"ghost","cpu":1,"mem":1}`); code != http.StatusNotFound {
		t.Fatalf("missing tenant: want 404, got %d", code)
	}
	// 任务超过容量
	if code, _ := do(t, "POST", srv.URL+"/tenants", `{"id":"x"}`); code != http.StatusCreated {
		t.Fatalf("setup create failed: %d", code)
	}
	if code, _ := do(t, "POST", srv.URL+"/tasks", `{"id":"huge","tenant":"x","cpu":99999,"mem":1}`); code != http.StatusBadRequest {
		t.Fatalf("huge task: want 400, got %d", code)
	}
}

func TestHTTP_MethodNotAllowed(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/healthz", "application/json", bytes.NewReader(nil))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST /healthz: want 405, got %d", resp.StatusCode)
	}
}
