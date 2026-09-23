package api

import (
	"encoding/json"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"metricsink/internal/model"
	"metricsink/internal/store"
	"metricsink/internal/synth"
)

const base int64 = 1735693200 // 2025-01-01T01:00:00Z

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	srv := NewServer(st)
	ts := httptest.NewServer(srv.Mux)
	t.Cleanup(func() {
		ts.Close()
		st.Close()
	})
	return srv, ts
}

func postJSON(t *testing.T, ts *httptest.Server, path, body string) (int, map[string]any) {
	t.Helper()
	resp, err := ts.Client().Post(ts.URL+path, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	return resp.StatusCode, out
}

func getJSON(t *testing.T, ts *httptest.Server, path string) map[string]any {
	t.Helper()
	resp, err := ts.Client().Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("GET %s -> %d", path, resp.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out
}

// TestHTTPIngestQueryRecompute 通过 HTTP 走完整链路：
// 摄入三种密度数据 → 查询分钟/小时层 → 与 source=recompute 对账。
func TestHTTPIngestQueryRecompute(t *testing.T) {
	_, ts := newTestServer(t)

	for _, d := range []synth.Density{synth.Dense, synth.Sparse, synth.Ragged} {
		samples := synth.Generate(synth.Options{
			Metric: "net.rx", Labels: map[string]string{"host": "h1"},
			IDPrefix: string(d), Start: base, End: base + 2*3600 - 1,
			Density: d, Seed: 7,
		})
		body, _ := json.Marshal(struct {
			Samples []model.Sample `json:"samples"`
		}{samples})
		status, out := postJSON(t, ts, "/v1/ingest", string(body))
		if status != 200 {
			t.Fatalf("密度 %s 摄入状态 %d: %v", d, status, out)
		}
	}

	end := model.FormatTs(base + 2*3600 - 1)
	start := model.FormatTs(base)
	for _, step := range []string{"60", "3600"} {
		stored := getJSON(t, ts, "/v1/query?metric=net.rx&label.host=h1&start="+start+
			"&end="+end+"&step="+step+"&fill=zero")
		recomp := getJSON(t, ts, "/v1/query?metric=net.rx&label.host=h1&start="+start+
			"&end="+end+"&step="+step+"&fill=zero&source=recompute")

		r1 := stored["results"].([]any)
		r2 := recomp["results"].([]any)
		if len(r1) != 1 || len(r2) != 1 {
			t.Fatalf("应只有一条序列: %v / %v", r1, r2)
		}
		p1 := r1[0].(map[string]any)["points"].([]any)
		p2 := r2[0].(map[string]any)["points"].([]any)
		if len(p1) != len(p2) {
			t.Fatalf("step=%s 点数 %d != 重算点数 %d", step, len(p1), len(p2))
		}
		var empties int
		for i := range p1 {
			a := p1[i].(map[string]any)
			b := p2[i].(map[string]any)
			if a["ts"] != b["ts"] || a["count"] != b["count"] {
				t.Fatalf("step=%s 桶 %v 与重算 %v 不一致", step, a, b)
			}
			if a["count"].(float64) == 0 {
				empties++
				if a["avg"] != nil {
					t.Fatalf("空桶 avg 应为 null: %v", a)
				}
			}
		}
		t.Logf("step=%s 桶数=%d 空桶=%d", step, len(p1), empties)
	}
}

// TestHTTPLateRevision HTTP 层验证迟到修订传播。
func TestHTTPLateRevision(t *testing.T) {
	_, ts := newTestServer(t)

	ingest := func(ids []string, vals []float64) {
		t.Helper()
		var smps []model.Sample
		for i := range ids {
			smps = append(smps, model.Sample{
				ID: ids[i], Metric: "m", Labels: map[string]string{"host": "h"},
				Ts: base + int64(i*10), Value: vals[i],
			})
		}
		b, _ := json.Marshal(struct {
			Samples []model.Sample `json:"samples"`
		}{smps})
		status, out := postJSON(t, ts, "/v1/ingest", string(b))
		if status != 200 {
			t.Fatalf("摄入失败 %d: %v", status, out)
		}
	}

	ingest([]string{"a", "b", "c"}, []float64{1, 2, 99})
	ingest([]string{"c"}, []float64{3}) // 迟到修订 c: 99 -> 3

	out := getJSON(t, ts, "/v1/query?metric=m&start="+model.FormatTs(base)+
		"&end="+model.FormatTs(base+59)+"&step=60")
	pts := out["results"].([]any)[0].(map[string]any)["points"].([]any)
	p := pts[0].(map[string]any)
	if p["count"].(float64) != 3 || p["sum"].(float64) != 6 ||
		p["max"].(float64) != 3 {
		t.Fatalf("修订后分钟桶错误: %v", p)
	}

	// 删除同样传播。
	status, delOut := postJSON(t, ts, "/v1/delete", `{"ids":["a"]}`)
	if status != 200 || delOut["deleted"].(float64) != 1 {
		t.Fatalf("删除失败: %d %v", status, delOut)
	}
	out = getJSON(t, ts, "/v1/query?metric=m&start="+model.FormatTs(base)+
		"&end="+model.FormatTs(base+59)+"&step=60")
	p = out["results"].([]any)[0].(map[string]any)["points"].([]any)[0].(map[string]any)
	if p["count"].(float64) != 2 || p["sum"].(float64) != 5 || p["min"].(float64) != 2 {
		t.Fatalf("删除后分钟桶错误: %v", p)
	}
}

// TestHTTPRejectionsAndErrors 非法输入返回明确错误。
func TestHTTPRejectionsAndErrors(t *testing.T) {
	_, ts := newTestServer(t)

	// 非法 JSON。
	status, out := postJSON(t, ts, "/v1/ingest", `{not json`)
	if status != 400 {
		t.Fatalf("状态=%d 期望 400", status)
	}
	if _, ok := out["error"]; !ok {
		t.Fatal("应返回 error 字段")
	}

	// 样本级别非法：metric 缺失 → 409，合法样本仍被接受。
	status, out = postJSON(t, ts, "/v1/ingest", `{"samples":[
		{"id":"good","metric":"m","labels":{"host":"h"},"ts":`+itoa(base)+`,"value":1},
		{"id":"bad","metric":"","ts":1,"value":1}
	]}`)
	if status != 409 {
		t.Fatalf("状态=%d 期望 409", status)
	}
	if out["inserted"].(float64) != 1 {
		t.Fatalf("合法样本应被接受: %v", out)
	}
	rj := out["rejected"].([]any)
	if len(rj) != 1 {
		t.Fatalf("应有 1 条拒绝: %v", rj)
	}

	// 查询参数错误。
	resp, err := ts.Client().Get(ts.URL + "/v1/query?metric=&start=1&end=2")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 400 {
		t.Fatalf("缺 metric 应 400, 得到 %d", resp.StatusCode)
	}
	resp.Body.Close()
}

// TestHTTPSeriesListing 标签选择器按序列过滤。
func TestHTTPSeriesListing(t *testing.T) {
	_, ts := newTestServer(t)
	body := `{"samples":[
		{"id":"1","metric":"m","labels":{"host":"a","zone":"z1"},"ts":BASE,"value":1},
		{"id":"2","metric":"m","labels":{"host":"b","zone":"z1"},"ts":BASE,"value":2},
		{"id":"3","metric":"m","labels":{"host":"a","zone":"z2"},"ts":BASE,"value":3}
	]}`
	body = strings.ReplaceAll(body, "BASE", itoa(base))
	if status, out := postJSON(t, ts, "/v1/ingest", body); status != 200 {
		t.Fatalf("摄入: %d %v", status, out)
	}

	out := getJSON(t, ts, "/v1/series?metric=m&label.zone=z1")
	if len(out["series"].([]any)) != 2 {
		t.Fatalf("zone=z1 应有 2 条序列: %v", out)
	}
	out = getJSON(t, ts, "/v1/series?metric=m&label.host=a")
	if len(out["series"].([]any)) != 2 {
		t.Fatalf("host=a 应有 2 条序列: %v", out)
	}
	out = getJSON(t, ts, "/v1/series?metric=m&label.host=a&label.zone=z2")
	if len(out["series"].([]any)) != 1 {
		t.Fatalf("host=a,zone=z2 应有 1 条序列: %v", out)
	}
}

func itoa(n int64) string {
	return strconv.FormatInt(n, 10)
}
