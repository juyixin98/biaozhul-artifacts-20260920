package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"counterreset/internal/store"
	"counterreset/internal/wal"
)

func newTestServer(t *testing.T, withWAL bool) (*Server, string) {
	t.Helper()
	st := store.New()
	var w *wal.WAL
	dir := t.TempDir()
	if withWAL {
		var err error
		w, err = wal.Open(filepath.Join(dir, "wal.jsonl"))
		if err != nil {
			t.Fatal(err)
		}
	}
	return NewServer(st, w), dir
}

func do(t *testing.T, h http.Handler, method, path, body string) (int, map[string]any) {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("响应不是 JSON: %s", rec.Body.String())
		}
	}
	return rec.Code, out
}

func TestHealth(t *testing.T) {
	s, _ := newTestServer(t, false)
	code, body := do(t, s.Handler(), "GET", "/healthz", "")
	if code != 200 || body["status"] != "ok" {
		t.Fatalf("health 异常: %d %v", code, body)
	}
}

func TestIngestAndQueryShuffled(t *testing.T) {
	s, _ := newTestServer(t, false)
	h := s.Handler()

	// 乱序 + 同值重复 + 冲突（ts=2000 保留 20）。
	body := `{
	  "labels": {"__name__":"m","instance":"i1"},
	  "samples": [
	    {"ts_ms":3000,"value":30},{"ts_ms":1000,"value":10},
	    {"ts_ms":2000,"value":20},{"ts_ms":2000,"value":20},
	    {"ts_ms":2000,"value":15}
	  ]
	}`
	code, resp := do(t, h, "POST", "/v1/ingest", body)
	if code != 200 {
		t.Fatalf("摄入失败: %d %v", code, resp)
	}
	ing := resp["ingest"].(map[string]any)
	if ing["unique_samples"].(float64) != 3 {
		t.Fatalf("唯一样本数异常: %v", ing)
	}
	// 批次内：1 个同值重复（ts=2000,value=20），1 个冲突（ts=2000,value=15）
	if ing["batch_duplicate_same"].(float64) != 1 || ing["batch_duplicate_conflicts"].(float64) != 1 {
		t.Fatalf("批次内去重统计异常: %v", ing)
	}

	// 查询 [1000,3000]，下界 = 10 + 10 = 20，速率 = 20/2s = 10/s。
	q := "/v1/increase?label=__name__=m&label=instance=i1&start_ms=1000&end_ms=3000&extrapolation=none"
	code, resp = do(t, h, "GET", q, "")
	if code != 200 {
		t.Fatalf("查询失败: %d %v", code, resp)
	}
	rep := resp["report"].(map[string]any)
	inc := rep["increase"].(map[string]any)
	if inc["point"].(float64) != 20 {
		t.Fatalf("point = %v, 期望 20", inc["point"])
	}
	if inc["upper"] != nil {
		t.Fatalf("无容量 upper 应为 null: %v", inc["upper"])
	}
	ratePS := rep["rate_per_second"].(map[string]any)
	if ratePS["point"].(float64) != 10 {
		t.Fatalf("rate = %v, 期望 10", ratePS["point"])
	}
}

func TestIngestNegativeRejectedAtomic(t *testing.T) {
	s, _ := newTestServer(t, false)
	h := s.Handler()
	body := `{
	  "labels": {"__name__":"m"},
	  "samples": [{"ts_ms":1000,"value":1},{"ts_ms":2000,"value":-3}]
	}`
	code, resp := do(t, h, "POST", "/v1/ingest", body)
	if code != 400 {
		t.Fatalf("负值应返回 400, 实际 %d", code)
	}
	if resp["rejected"] == nil {
		t.Fatalf("应返回 rejected 明细: %v", resp)
	}
	// 整批拒绝：序列列表为空。
	code, resp = do(t, h, "GET", "/v1/series", "")
	if code != 200 {
		t.Fatalf("series 查询失败: %d", code)
	}
	if n := len(resp["series"].([]any)); n != 0 {
		t.Fatalf("整批拒绝后不应有序列，实际 %d", n)
	}
}

func TestIngestBadJSONAndNaN(t *testing.T) {
	s, _ := newTestServer(t, false)
	h := s.Handler()
	if code, _ := do(t, h, "POST", "/v1/ingest", "{not json"); code != 400 {
		t.Fatalf("坏 JSON 应 400, 实际 %d", code)
	}
	body := `{"labels":{"__name__":"m"},"samples":[{"ts_ms":1000,"value":"10"}]}`
	if code, _ := do(t, h, "POST", "/v1/ingest", body); code != 400 {
		t.Fatalf("类型错误应 400（DisallowUnknownFields + 严格解码）, 实际 %d", code)
	}
}

func TestQueryResetWithCapacity(t *testing.T) {
	s, _ := newTestServer(t, false)
	h := s.Handler()
	// 0→90→20：第二个区间观测到一次重置。
	body := `{"labels":{"__name__":"c"},"samples":[
	  {"ts_ms":1000000000000,"value":0},
	  {"ts_ms":1000000001000,"value":90},
	  {"ts_ms":1000000002000,"value":20}
	]}`
	if code, resp := do(t, h, "POST", "/v1/ingest", body); code != 200 {
		t.Fatalf("摄入失败: %d %v", code, resp)
	}
	// 下界：90 + 20 = 110；C=100 条件上界：(100-0+90)+(100-90+20)=220。
	q := "/v1/increase?label=__name__=c&start_ms=1000000000000&end_ms=1000000002000&extrapolation=none&capacity=100"
	code, resp := do(t, h, "GET", q, "")
	if code != 200 {
		t.Fatalf("查询失败: %d %v", code, resp)
	}
	inc := resp["report"].(map[string]any)["increase"].(map[string]any)
	if inc["point"].(float64) != 110 {
		t.Fatalf("point = %v, 期望 110", inc["point"])
	}
	if inc["upper"].(float64) != 220 {
		t.Fatalf("upper = %v, 期望 220", inc["upper"])
	}
}

func TestQueryErrorsAndMissingSeries(t *testing.T) {
	s, _ := newTestServer(t, false)
	h := s.Handler()
	if code, _ := do(t, h, "GET", "/v1/increase?start_ms=1&end_ms=2", ""); code != 400 {
		t.Fatalf("缺 label 应 400, 实际 %d", code)
	}
	q := "/v1/increase?label=__name__=nope&start_ms=1&end_ms=2"
	if code, _ := do(t, h, "GET", q, ""); code != 404 {
		t.Fatalf("不存在的序列应 404, 实际 %d", code)
	}
	q2 := "/v1/rate?label=a=b&start_ms=10&end_ms=5"
	// 序列不存在时优先返回 404（标签解析先于窗口校验）。
	if code, _ := do(t, h, "GET", q2, ""); code != 404 {
		t.Fatalf("不存在序列的非法窗口请求应 404, 实际 %d", code)
	}
	if code, _ := do(t, h, "POST", "/v1/rate?label=a=b", ""); code != 405 {
		t.Fatalf("POST /v1/rate 应 405, 实际 %d", code)
	}
	if code, _ := do(t, h, "DELETE", "/healthz", ""); code != 405 {
		t.Fatalf("DELETE /healthz 应 405, 实际 %d", code)
	}
}

func TestWALReplayAcrossServerRestart(t *testing.T) {
	srv, dir := newTestServer(t, true)
	h := srv.Handler()
	body := `{"labels":{"__name__":"p"},"samples":[
	  {"ts_ms":1000,"value":1},{"ts_ms":3000,"value":3},{"ts_ms":2000,"value":2}
	]}`
	if code, resp := do(t, h, "POST", "/v1/ingest", body); code != 200 {
		t.Fatalf("摄入失败: %d %v", code, resp)
	}
	if err := srv.WAL.Close(); err != nil {
		t.Fatal(err)
	}

	// 用同一个 WAL 文件重新构建存储（模拟重启）。
	st2 := store.New()
	w2, err := wal.Open(filepath.Join(dir, "wal.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	ok, bad, err := wal.Replay(filepath.Join(dir, "wal.jsonl"), func(r wal.Record) error {
		_, rejected, ierr := st2.Ingest(r.Labels, r.Samples)
		if len(rejected) > 0 {
			t.Fatalf("重放出现非法样本: %v", rejected)
		}
		return ierr
	})
	if err != nil {
		t.Fatal(err)
	}
	if ok != 1 || bad != 0 {
		t.Fatalf("重放计数异常 ok=%d bad=%d", ok, bad)
	}
	srv2 := NewServer(st2, w2)
	q := "/v1/increase?label=__name__=p&start_ms=1000&end_ms=3000&extrapolation=none"
	code, resp := do(t, srv2.Handler(), "GET", q, "")
	if code != 200 {
		t.Fatalf("重启后查询失败: %d %v", code, resp)
	}
	point := resp["report"].(map[string]any)["increase"].(map[string]any)["point"]
	if point.(float64) != 2 {
		t.Fatalf("重启后 point = %v, 期望 2（乱序已重排）", point)
	}
}

func TestRateEndpointShape(t *testing.T) {
	s, _ := newTestServer(t, false)
	h := s.Handler()
	body := `{"labels":{"__name__":"r"},"samples":[
	  {"ts_ms":1000,"value":0},{"ts_ms":2000,"value":10},{"ts_ms":3000,"value":20}
	]}`
	if code, resp := do(t, h, "POST", "/v1/ingest", body); code != 200 {
		t.Fatalf("摄入失败: %d %v", code, resp)
	}
	q := "/v1/rate?label=__name__=r&start_ms=1000&end_ms=3000&extrapolation=linear"
	code, resp := do(t, h, "GET", q, "")
	if code != 200 {
		t.Fatalf("rate 查询失败: %d %v", code, resp)
	}
	rep := resp["report"].(map[string]any)
	if rep["rate_per_second"].(map[string]any)["point"].(float64) != 10 {
		t.Fatalf("rate 异常: %v", rep["rate_per_second"])
	}

	// 序列存在时，非法窗口与未知策略应返回 400。
	badQ := "/v1/rate?label=__name__=r&start_ms=3000&end_ms=1000"
	if code, _ := do(t, h, "GET", badQ, ""); code != 400 {
		t.Fatalf("非法窗口应 400, 实际 %d", code)
	}
	badExtrap := "/v1/rate?label=__name__=r&start_ms=1000&end_ms=3000&extrapolation=guess"
	if code, _ := do(t, h, "GET", badExtrap, ""); code != 400 {
		t.Fatalf("未知策略应 400, 实际 %d", code)
	}
	badCap := "/v1/rate?label=__name__=r&start_ms=1000&end_ms=3000&capacity=-1"
	if code, _ := do(t, h, "GET", badCap, ""); code != 400 {
		t.Fatalf("非法容量应 400, 实际 %d", code)
	}
}
