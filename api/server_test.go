package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"quorum-demo/quorum"
)

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	c, err := quorum.New(quorum.Config{N: 3, W: 2, R: 2},
		quorum.WithTiming(2*time.Millisecond, 8*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	s := NewServer(c)
	return s, httptest.NewServer(s.Handler())
}

func postJSON(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("响应不是 JSON: %v", err)
	}
	return resp.StatusCode, out
}

func TestHealth(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()
	resp, err := http.Get(ts.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("health 状态码 %d", resp.StatusCode)
	}
}

// 端到端验收场景：部分写超时（504）→ 恢复 → 读修复 → 读到真实版本。
func TestHTTPPartialWriteThenRecovery(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	if st, body := postJSON(t, ts.URL+"/replicas/down", map[string]string{"node": "n2"}); st != 200 || body["down"] == nil {
		t.Fatalf("宕机 n2 失败: %d %v", st, body)
	}
	if st, _ := postJSON(t, ts.URL+"/replicas/down", map[string]string{"node": "n3"}); st != 200 {
		t.Fatalf("宕机 n3 失败: %d", st)
	}

	st, body := postJSON(t, ts.URL+"/write", map[string]string{"key": "k", "value": "v1", "coordinator": "n1"})
	if st != http.StatusGatewayTimeout {
		t.Fatalf("W=2 仅 1 在线应返回 504, got %d: %v", st, body)
	}
	if met, _ := body["quorum_met"].(bool); met {
		t.Fatalf("quorum_met 应为 false")
	}
	acked, _ := body["acked_by"].([]any)
	if len(acked) != 1 || acked[0] != "n1" {
		t.Fatalf("只有 n1 落地部分写: %v", acked)
	}

	// 法定人数不足时的读同样应是 504。
	if st, body := postJSON(t, ts.URL+"/read", map[string]string{"key": "k"}); st != http.StatusGatewayTimeout {
		t.Fatalf("R=2 仅 1 在线应 504, got %d %v", st, body)
	}

	// 恢复副本，随后 R=2 读返回唯一版本并触发修复（200）。
	if st, _ := postJSON(t, ts.URL+"/replicas/up", map[string]string{"node": "n2"}); st != 200 {
		t.Fatalf("恢复 n2 失败: %d", st)
	}
	if st, _ := postJSON(t, ts.URL+"/replicas/up", map[string]string{"node": "n3"}); st != 200 {
		t.Fatalf("恢复 n3 失败: %d", st)
	}
	st, body = postJSON(t, ts.URL+"/read", map[string]string{"key": "k"})
	if st != 200 {
		t.Fatalf("恢复后读应 200, got %d: %v", st, body)
	}
	versions, _ := body["versions"].([]any)
	if len(versions) != 1 {
		t.Fatalf("应读到唯一版本, got %v", versions)
	}
	v0, _ := versions[0].(map[string]any)
	if v0["value"] != "v1" {
		t.Fatalf("读到值应为 v1: %v", v0)
	}
	repaired, _ := body["repaired_to"].([]any)
	if len(repaired) != 2 {
		t.Fatalf("应修复 n2/n3: %v", repaired)
	}
}

// 端到端验收场景：并发写 → 409 冲突（返回两个兄弟版本）→ resolve 消解。
func TestHTTPConcurrentConflictAndResolve(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	st, wa := postJSON(t, ts.URL+"/write",
		map[string]any{"key": "k", "value": "A", "coordinator": "n1", "nodes": []string{"n1", "n2"}})
	if st != 200 {
		t.Fatalf("写 A 失败: %d %v", st, wa)
	}
	st, wb := postJSON(t, ts.URL+"/write",
		map[string]any{"key": "k", "value": "B", "coordinator": "n2", "nodes": []string{"n2", "n3"}})
	if st != 200 {
		t.Fatalf("写 B 失败: %d %v", st, wb)
	}

	st, body := postJSON(t, ts.URL+"/read", map[string]string{"key": "k"})
	if st != http.StatusConflict {
		t.Fatalf("并发兄弟版本应返回 409, got %d", st)
	}
	if conflict, _ := body["conflict"].(bool); !conflict {
		t.Fatalf("conflict 应为 true")
	}
	versions, _ := body["versions"].([]any)
	if len(versions) != 2 {
		t.Fatalf("应返回 2 个兄弟版本: %v", versions)
	}
	note, _ := body["note"].(string)
	if !strings.Contains(note, "线性一致") {
		t.Fatalf("冲突响应必须明确声明不构成线性一致: %q", note)
	}

	// 取两个兄弟钟做 resolve。
	clockA := versions[0].(map[string]any)["clock"].(map[string]any)
	clockB := versions[1].(map[string]any)["clock"].(map[string]any)
	st, res := postJSON(t, ts.URL+"/resolve", map[string]any{
		"key":         "k",
		"value":       "winner",
		"coordinator": "n3",
		"siblings":    []any{clockA, clockB},
	})
	if st != 200 || res["quorum_met"] != true {
		t.Fatalf("resolve 应成功: %d %v", st, res)
	}
	st, body = postJSON(t, ts.URL+"/read", map[string]string{"key": "k"})
	if st != 200 {
		t.Fatalf("消解后读应 200, got %d %v", st, body)
	}
	versions, _ = body["versions"].([]any)
	if len(versions) != 1 || versions[0].(map[string]any)["value"] != "winner" {
		t.Fatalf("消解后应只剩 winner: %v", versions)
	}
}

// 读不存在 key：404；坏 JSON：400。
func TestHTTPNotFoundAndBadRequest(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	st, body := postJSON(t, ts.URL+"/read", map[string]string{"key": "missing"})
	if st != http.StatusNotFound {
		t.Fatalf("缺失 key 应 404, got %d %v", st, body)
	}

	resp, err := http.Post(ts.URL+"/write", "application/json", strings.NewReader("{not json"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("坏 JSON 应 400, got %d", resp.StatusCode)
	}
}

// /config 可在运行期调整 W/R，并在 W+R<=N 时给出旧读风险提示。
func TestHTTPReconfigure(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()
	st, body := postJSON(t, ts.URL+"/config", map[string]int{"w": 1, "r": 1})
	if st != 200 {
		t.Fatalf("重配失败: %d %v", st, body)
	}
	note, _ := body["note"].(string)
	if !strings.Contains(note, "<=") {
		t.Fatalf("W+R<=N 时应提示旧读风险: %q", note)
	}
	st, _ = postJSON(t, ts.URL+"/config", map[string]int{"w": 0, "r": 2})
	if st != http.StatusBadRequest {
		t.Fatalf("非法 W 应 400, got %d", st)
	}
}
