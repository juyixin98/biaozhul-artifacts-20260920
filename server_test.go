package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(newServer().routes())
	t.Cleanup(srv.Close)
	return srv
}

func doJSON(t *testing.T, method, url string, body string) (int, map[string]any) {
	t.Helper()
	var reader *bytes.Reader
	if body == "" {
		reader = bytes.NewReader(nil)
	} else {
		reader = bytes.NewReader([]byte(body))
	}
	req, err := http.NewRequest(method, url, reader)
	if err != nil {
		t.Fatal(err)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	return resp.StatusCode, out
}

const exampleCluster = `{
  "devices": [
    {"id": "gpu0", "memoryMB": 24576, "numaNode": 0},
    {"id": "gpu1", "memoryMB": 24576, "numaNode": 0},
    {"id": "gpu2", "memoryMB": 24576, "numaNode": 1},
    {"id": "gpu3", "memoryMB": 24576, "numaNode": 1}
  ],
  "defaultSameNUMACost": 1,
  "defaultCrossNUMACost": 10
}`

func TestEndToEndFlow(t *testing.T) {
	srv := newTestServer(t)

	// 健康检查。
	code, body := doJSON(t, http.MethodGet, srv.URL+"/healthz", "")
	if code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("健康检查失败: %d %+v", code, body)
	}
	if body["clusterConfigured"] != false {
		t.Fatalf("未配置时 clusterConfigured 应为 false: %+v", body)
	}

	// 未配置就分配 → 409 NO_CLUSTER。
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"early","replicas":1,"memoryPerReplicaMB":1024}`)
	if code != http.StatusConflict || body["reason"] != "NO_CLUSTER" {
		t.Fatalf("未配置应 409 NO_CLUSTER: %d %+v", code, body)
	}

	// 配置集群。
	code, body = doJSON(t, http.MethodPut, srv.URL+"/api/cluster", exampleCluster)
	if code != http.StatusOK || body["configured"] != true {
		t.Fatalf("配置集群失败: %d %+v", code, body)
	}

	// 2 卡任务 → 应落在同一 NUMA（gpu0+gpu1），代价 1。
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"train-1","replicas":2,"memoryPerReplicaMB":8192}`)
	if code != http.StatusCreated {
		t.Fatalf("分配应 201: %d %+v", code, body)
	}
	if body["cost"] != 1.0 || body["crossNUMAPairs"] != 0.0 || body["sameNUMAPairs"] != 1.0 {
		t.Fatalf("应同 NUMA 放置: %+v", body)
	}
	if body["exhaustive"] != true {
		t.Fatalf("小集群应为穷举最优: %+v", body)
	}
	assignments := body["assignments"].([]any)
	got := map[string]bool{}
	for _, a := range assignments {
		got[a.(map[string]any)["deviceId"].(string)] = true
	}
	if !got["gpu0"] || !got["gpu1"] {
		t.Fatalf("应分配 gpu0+gpu1: %v", got)
	}

	// 再来一个 2 卡任务 → 落在 gpu2+gpu3。
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"train-2","replicas":2,"memoryPerReplicaMB":8192}`)
	if code != http.StatusCreated {
		t.Fatalf("第二个任务应 201: %d %+v", code, body)
	}

	// 集群已满（每卡剩 16GB，但 4 卡都被占用显存）——
	// 申请 4×24GB：总剩余 64GB < 96GB → INSUFFICIENT_MEMORY。
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"too-big","replicas":4,"memoryPerReplicaMB":24576}`)
	if code != http.StatusUnprocessableEntity || body["reason"] != "INSUFFICIENT_MEMORY" {
		t.Fatalf("应 422 INSUFFICIENT_MEMORY: %d %+v", code, body)
	}

	// 申请 5 张卡 → NOT_ENOUGH_DEVICES。
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"too-many","replicas":5,"memoryPerReplicaMB":1024}`)
	if code != http.StatusUnprocessableEntity || body["reason"] != "NOT_ENOUGH_DEVICES" {
		t.Fatalf("应 422 NOT_ENOUGH_DEVICES: %d %+v", code, body)
	}

	// 任务列表与详情。
	code, body = doJSON(t, http.MethodGet, srv.URL+"/api/tasks", "")
	if code != http.StatusOK || len(body["tasks"].([]any)) != 2 {
		t.Fatalf("任务列表应有 2 项: %d %+v", code, body)
	}
	code, body = doJSON(t, http.MethodGet, srv.URL+"/api/tasks/train-1", "")
	if code != http.StatusOK || body["taskId"] != "train-1" {
		t.Fatalf("任务详情失败: %d %+v", code, body)
	}

	// 释放后详情 404。
	code, _ = doJSON(t, http.MethodDelete, srv.URL+"/api/tasks/train-1", "")
	if code != http.StatusOK {
		t.Fatalf("释放失败: %d", code)
	}
	code, _ = doJSON(t, http.MethodGet, srv.URL+"/api/tasks/train-1", "")
	if code != http.StatusNotFound {
		t.Fatalf("释放后应 404: %d", code)
	}

	// 释放 train-1 后，4×8GB 任务可以放下（验证状态回收）。
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"train-3","replicas":4,"memoryPerReplicaMB":8192}`)
	if code != http.StatusCreated {
		t.Fatalf("释放后 4 卡任务应成功: %d %+v", code, body)
	}
}

func TestBadRequests(t *testing.T) {
	srv := newTestServer(t)

	// 非法 JSON。
	code, body := doJSON(t, http.MethodPut, srv.URL+"/api/cluster", `{not json`)
	if code != http.StatusBadRequest {
		t.Fatalf("非法 JSON 应 400: %d %+v", code, body)
	}

	// 未知字段（严格解码）。
	code, _ = doJSON(t, http.MethodPut, srv.URL+"/api/cluster",
		`{"devices":[{"id":"g0","memoryMB":1024,"numaNode":0}],"typo":1}`)
	if code != http.StatusBadRequest {
		t.Fatalf("未知字段应 400: %d", code)
	}

	// 拓扑校验失败（重复 id）。
	code, body = doJSON(t, http.MethodPut, srv.URL+"/api/cluster",
		`{"devices":[{"id":"g0","memoryMB":1024,"numaNode":0},{"id":"g0","memoryMB":1024,"numaNode":0}]}`)
	if code != http.StatusBadRequest || !strings.Contains(body["error"].(map[string]any)["message"].(string), "重复") {
		t.Fatalf("重复 id 应 400 且说明原因: %d %+v", code, body)
	}

	// 非法任务参数。
	doJSON(t, http.MethodPut, srv.URL+"/api/cluster", exampleCluster)
	code, body = doJSON(t, http.MethodPost, srv.URL+"/api/tasks",
		`{"taskId":"bad","replicas":0,"memoryPerReplicaMB":1024}`)
	if code != http.StatusBadRequest || body["reason"] != "INVALID_TASK" {
		t.Fatalf("非法参数应 400 INVALID_TASK: %d %+v", code, body)
	}

	// 不存在的任务。
	code, _ = doJSON(t, http.MethodGet, srv.URL+"/api/tasks/ghost", "")
	if code != http.StatusNotFound {
		t.Fatalf("不存在任务应 404: %d", code)
	}
	code, _ = doJSON(t, http.MethodDelete, srv.URL+"/api/tasks/ghost", "")
	if code != http.StatusNotFound {
		t.Fatalf("删除不存在任务应 404: %d", code)
	}
}
