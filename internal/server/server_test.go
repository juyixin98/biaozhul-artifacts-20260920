package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"tracestitch/internal/assembler"
	"tracestitch/internal/clock"
	"tracestitch/internal/model"
)

func newTestSrv(t *testing.T) (*httptest.Server, *clock.Fake) {
	t.Helper()
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	fc := clock.NewFake(base)
	asm := assembler.New(assembler.DefaultConfig(), fc, nil, nil)
	return httptest.NewServer(New(asm).Handler()), fc
}

func doJSON(t *testing.T, srv *httptest.Server, method, path string, body any, out any) *http.Response {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.Config.Handler.ServeHTTP(w, req)
	res := w.Result()
	defer res.Body.Close()
	if out != nil && strings.Contains(res.Header.Get("Content-Type"), "json") {
		if err := json.NewDecoder(res.Body).Decode(out); err != nil {
			t.Fatalf("decode %s: %v (body recorded)", path, err)
		}
	}
	return res
}

// 端到端：乱序批量摄入 -> 查询 -> 冲突 -> 超时 -> 迟到补全 -> 包含关系。
func TestHTTPEndToEndAssembly(t *testing.T) {
	srv, _ := newTestSrv(t)
	defer srv.Close()
	const trace = "E2E"

	mk := func(id, parent, svc, name string, start, end int64) model.Span {
		return model.Span{
			TraceID: trace, SpanID: id, ParentSpanID: parent,
			ServiceName: svc, Name: name,
			StartUnixNano: start, EndUnixNano: end,
		}
	}

	// 1) 子先父后批量到达（孙 -> 子，根仍缺）。
	batch := map[string]any{"spans": []model.Span{
		mk("leaf", "mid", "svc-leaf", "leaf-op", 100, 200),
		mk("mid", "root", "svc-mid", "mid-op", 50, 9_000),
	}}
	var ing ingestBatchResponse
	res := doJSON(t, srv, http.MethodPost, "/v1/spans", batch, &ing)
	if res.StatusCode != http.StatusOK {
		t.Fatalf("ingest status=%d", res.StatusCode)
	}
	if len(ing.Results) != 2 {
		t.Fatalf("results=%d want 2", len(ing.Results))
	}
	for _, r := range ing.Results {
		if r.Status != "accepted" {
			t.Fatalf("unexpected status %s: %+v", r.Status, r)
		}
	}

	// 2) 查询：缺根、不完整。
	var view model.TraceView
	if res := doJSON(t, srv, http.MethodGet, "/v1/traces/"+trace, nil, &view); res.StatusCode != http.StatusOK {
		t.Fatalf("get trace status=%d", res.StatusCode)
	}
	if view.Complete {
		t.Fatal("trace should be incomplete while root missing")
	}
	if view.Latest == nil || !view.Latest.MissingRoot {
		t.Fatalf("latest revision must flag missingRoot: %+v", view.Latest)
	}

	// 3) 冲突重复：同一 spanId 不同 name。
	bad := mk("mid", "root", "svc-mid", "MUTATED-NAME", 50, 9_000)
	var conflict assembler.IngestResult
	if res := doJSON(t, srv, http.MethodPost, "/v1/span", bad, &conflict); res.StatusCode != http.StatusOK {
		t.Fatalf("conflict ingest status=%d", res.StatusCode)
	}
	if conflict.Status != "conflict" || conflict.Revision == 0 {
		t.Fatalf("conflict result=%+v", conflict)
	}

	// 4) 强制超时输出不完整修订（demo/测试用确定性入口）。
	if res := doJSON(t, srv, http.MethodPost, "/admin/traces/"+trace+"/flush", nil, &view); res.StatusCode != http.StatusOK {
		t.Fatalf("flush status=%d", res.StatusCode)
	}
	if !view.Sealed || view.Latest.Complete {
		t.Fatalf("after flush sealed=%v complete=%v, want sealed & incomplete", view.Sealed, view.Latest.Complete)
	}
	if view.Latest.Reason != assembler.ReasonTimeout {
		t.Fatalf("latest reason=%s want timeout", view.Latest.Reason)
	}

	// 5) 根迟到 -> 新修订补全。
	root := mk("root", "", "svc-root", "root-op", 10_000, 20_000)
	var late assembler.IngestResult
	doJSON(t, srv, http.MethodPost, "/v1/span", root, &late)
	if late.Reason != assembler.ReasonLate || late.Revision == 0 {
		t.Fatalf("late result=%+v", late)
	}

	// 6) 修订历史与包含关系。
	var revs struct {
		Revisions []model.Revision `json:"revisions"`
	}
	doJSON(t, srv, http.MethodGet, "/v1/traces/"+trace+"/revisions", nil, &revs)
	if len(revs.Revisions) < 4 {
		t.Fatalf("revisions=%d want >=4 (initial,extended,conflict,timeout,late)", len(revs.Revisions))
	}
	last := revs.Revisions[len(revs.Revisions)-1]
	if !last.Complete {
		t.Fatal("last revision must be complete")
	}
	// 每个修订的 spanId 集合必须包含前一版：leaf,mid -> leaf,mid -> ...
	for i := 1; i < len(revs.Revisions); i++ {
		prev := revs.Revisions[i-1].SpanIDs
		cur := map[string]bool{}
		for _, id := range revs.Revisions[i].SpanIDs {
			cur[id] = true
		}
		for _, id := range prev {
			if !cur[id] {
				t.Fatalf("revision %d lost span %s present in revision %d", i+1, id, i)
			}
		}
	}

	var containment struct {
		Holds     bool                            `json:"holds"`
		Violation *assembler.ContainmentViolation `json:"violation"`
	}
	if res := doJSON(t, srv, http.MethodGet, "/v1/traces/"+trace+"/containment", nil, &containment); res.StatusCode != http.StatusOK {
		t.Fatalf("containment status=%d", res.StatusCode)
	}
	if !containment.Holds {
		t.Fatalf("containment violation: %+v", containment.Violation)
	}

	// 7) 因果只看引用：root 是唯一林根，结构 root->mid->leaf（时间戳再乱也不变）。
	doJSON(t, srv, http.MethodGet, "/v1/traces/"+trace, nil, &view)
	if len(view.Forest) != 1 || view.Forest[0].Span.SpanID != "root" {
		t.Fatalf("forest=%v want single root 'root'", forestIDs(view.Forest))
	}
	midNode := view.Forest[0].Children[0]
	if midNode.Span.SpanID != "mid" || midNode.Children[0].Span.SpanID != "leaf" {
		t.Fatalf("assembled tree wrong: %s -> %v", view.Forest[0].Span.SpanID, forestIDs(midChildren(midNode)))
	}
	// 首条为准：冲突的 MUTATED-NAME 不能出现。
	if midNode.Span.Name != "mid-op" {
		t.Fatalf("first-write-wins violated: name=%s", midNode.Span.Name)
	}

	// 8) 已完成的 trace 不能再 flush。
	w := httptest.NewRecorder()
	srv.Config.Handler.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/admin/traces/"+trace+"/flush", nil))
	if w.Code != http.StatusConflict {
		t.Fatalf("flush completed trace status=%d want 409", w.Code)
	}
}

func TestHTTPErrors(t *testing.T) {
	srv, _ := newTestSrv(t)
	defer srv.Close()

	// 非法 JSON。
	req := httptest.NewRequest(http.MethodPost, "/v1/spans", strings.NewReader("{not json"))
	w := httptest.NewRecorder()
	srv.Config.Handler.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("bad json status=%d want 400", w.Code)
	}

	// 空批次。
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/v1/spans", strings.NewReader(`{"spans":[]}`))
	srv.Config.Handler.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("empty batch status=%d want 400", w.Code)
	}

	// 全无效批次。
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/v1/spans", strings.NewReader(`{"spans":[{"name":"x"}]}`))
	srv.Config.Handler.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("all-invalid batch status=%d want 400", w.Code)
	}

	// 查询不存在的 trace。
	w = httptest.NewRecorder()
	srv.Config.Handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/v1/traces/nope", nil))
	if w.Code != http.StatusNotFound {
		t.Fatalf("missing trace status=%d want 404", w.Code)
	}

	// 不存在的修订号。
	w = httptest.NewRecorder()
	srv.Config.Handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/v1/traces/nope/revisions/x", nil))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("bad revision status=%d want 400", w.Code)
	}
}

func TestClockSkewOverHTTP(t *testing.T) {
	srv, _ := newTestSrv(t)
	defer srv.Close()
	const trace = "SKEW"
	mk := func(id, parent, svc, name string, start, end int64) model.Span {
		return model.Span{TraceID: trace, SpanID: id, ParentSpanID: parent,
			ServiceName: svc, Name: name, StartUnixNano: start, EndUnixNano: end}
	}
	batch := map[string]any{"spans": []model.Span{
		mk("parent", "", "gateway", "parent", 10_000_000, 50_000_000),
		mk("child", "parent", "payments", "child", 1_000_000, 8_000_000), // 时钟慢 9ms
	}}
	doJSON(t, srv, http.MethodPost, "/v1/spans", batch, nil)

	var view model.TraceView
	doJSON(t, srv, http.MethodGet, "/v1/traces/"+trace, nil, &view)
	if !view.Complete {
		t.Fatal("skew must not make trace incomplete")
	}
	if len(view.Latest.ClockSkew) != 1 ||
		view.Latest.ClockSkew[0].Kind != "child-starts-before-parent" {
		t.Fatalf("skew warnings=%+v", view.Latest.ClockSkew)
	}
	// 但树仍按引用：child 在 parent 下。
	if len(view.Forest) != 1 || view.Forest[0].Span.SpanID != "parent" ||
		len(view.Forest[0].Children) != 1 || view.Forest[0].Children[0].Span.SpanID != "child" {
		t.Fatal("causality must follow references, not skewed clocks")
	}
}

func forestIDs(ns []*model.SpanNode) []string {
	var out []string
	for _, n := range ns {
		out = append(out, n.Span.SpanID)
	}
	return out
}

func midChildren(m *model.SpanNode) []*model.SpanNode { return m.Children }
