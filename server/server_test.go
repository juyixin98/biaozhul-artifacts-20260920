package server

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

const testMaxWait = 20 * time.Millisecond

func newTestServer(t *testing.T) *Server {
	t.Helper()
	srv, err := New(Config{
		Addr:          "127.0.0.1:0",
		MaxCount:      4,
		MaxBatchBytes: 4096,
		MaxItemBytes:  512,
		MaxWait:       testMaxWait,
		ExecLatency:   2 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	})
	return srv
}

func postJSON(t *testing.T, h http.Handler, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/infer", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestInfer_BatchingAndIndependentResults(t *testing.T) {
	srv := newTestServer(t)

	var wg sync.WaitGroup
	type resp struct {
		status int
		body   map[string]any
	}
	resps := make([]resp, 5)
	start := make(chan struct{})
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			body := fmt.Sprintf(`{"id":"r%d","model":"llm-a","prompt":"hello-%d"}`, i, i)
			resps[i].status, resps[i].body = postJSON(t, srv.httpSrv.Handler, body)
		}(i)
	}
	close(start)
	wg.Wait()

	for i, r := range resps {
		if r.status != http.StatusOK {
			t.Fatalf("request %d status = %d body = %v", i, r.status, r.body)
		}
		if r.body["status"] != "ok" {
			t.Fatalf("request %d status field = %v", i, r.body["status"])
		}
		result := r.body["result"].(map[string]any)
		if result["id"] != fmt.Sprintf("r%d", i) {
			t.Fatalf("request %d got result id %v", i, result["id"])
		}
	}
	// 5 条、MaxCount=4 -> 恰好两批。
	if srv.Executor().BatchCount() != 2 {
		t.Fatalf("batches executed = %d, want 2", srv.Executor().BatchCount())
	}
}

func TestInfer_ModelKeyIsolation(t *testing.T) {
	srv := newTestServer(t)

	type resp struct {
		status int
		body   map[string]any
	}
	ch := make(chan resp, 2)
	go func() {
		s, b := postJSON(t, srv.httpSrv.Handler, `{"id":"a","model":"model-a","prompt":"p"}`)
		ch <- resp{s, b}
	}()
	go func() {
		s, b := postJSON(t, srv.httpSrv.Handler, `{"id":"b","model":"model-b","prompt":"p"}`)
		ch <- resp{s, b}
	}()
	for i := 0; i < 2; i++ {
		select {
		case r := <-ch:
			if r.status != http.StatusOK || r.body["status"] != "ok" {
				t.Fatalf("unexpected response: %d %v", r.status, r.body)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("requests did not complete")
		}
	}
	// 两个模型各开一批（超时发批）。
	st := srv.Batcher().Stats()
	if st.Flushed < 2 {
		t.Fatalf("flushed = %d, want >= 2 (keys isolated)", st.Flushed)
	}
}

func TestInfer_PartialFailureResponse(t *testing.T) {
	srv := newTestServer(t)

	var wg sync.WaitGroup
	wg.Add(2)
	results := make(chan map[string]any, 2)
	statuses := make(chan int, 2)
	go func() {
		defer wg.Done()
		s, b := postJSON(t, srv.httpSrv.Handler,
			`{"id":"bad","model":"m","prompt":"p","simulate_error":"model_error"}`)
		statuses <- s
		results <- b
	}()
	go func() {
		defer wg.Done()
		s, b := postJSON(t, srv.httpSrv.Handler, `{"id":"good","model":"m","prompt":"p"}`)
		statuses <- s
		results <- b
	}()
	wg.Wait()
	close(results)
	close(statuses)

	badCount, goodCount := 0, 0
	for b := range results {
		switch b["request_id"] {
		case "bad":
			if b["status"] != "item_error" {
				t.Fatalf("bad item status = %v", b["status"])
			}
			e := b["error"].(map[string]any)
			if e["code"] != "model_error" {
				t.Fatalf("bad item code = %v", e["code"])
			}
			badCount++
		case "good":
			if b["status"] != "ok" {
				t.Fatalf("good item status = %v, body = %v", b["status"], b)
			}
			goodCount++
		}
	}
	if badCount != 1 || goodCount != 1 {
		t.Fatalf("counts bad=%d good=%d", badCount, goodCount)
	}
}

func TestInfer_OversizedRejected(t *testing.T) {
	srv := newTestServer(t)
	bigPrompt := strings.Repeat("x", 4096) // Size = 64 + len(model) + prompt
	status, body := postJSON(t, srv.httpSrv.Handler,
		fmt.Sprintf(`{"id":"big","model":"m","prompt":"%s"}`, bigPrompt))
	if status != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413; body=%v", status, body)
	}
	if body["error"].(map[string]any)["code"] != "item_too_large" {
		t.Fatalf("error code = %v", body["error"])
	}
}

func TestInfer_ValidationErrors(t *testing.T) {
	srv := newTestServer(t)
	cases := map[string]int{
		`{not json`:      http.StatusBadRequest,
		`{"prompt":"p"}`: http.StatusBadRequest, // missing model
		`{"model":"m"}`:  http.StatusBadRequest, // missing prompt
	}
	for body, want := range cases {
		status, parsed := postJSON(t, srv.httpSrv.Handler, body)
		if status != want {
			t.Fatalf("body=%s status=%d want %d resp=%v", body, status, want, parsed)
		}
	}
}

func TestHealthAndMetrics(t *testing.T) {
	srv := newTestServer(t)
	h := srv.httpSrv.Handler

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz = %d", rec.Code)
	}

	_, _ = postJSON(t, h, `{"id":"m1","model":"m","prompt":"p"}`)
	time.Sleep(2 * testMaxWait)

	req = httptest.NewRequest(http.MethodGet, "/v1/metrics", nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("metrics = %d", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	sched := body["scheduler"].(map[string]any)
	if sched["flushed"].(float64) < 1 {
		t.Fatalf("flushed = %v, want >= 1", sched["flushed"])
	}
}

func TestEventsSSE(t *testing.T) {
	srv := newTestServer(t)
	ts := httptest.NewServer(srv.httpSrv.Handler)
	defer ts.Close()

	// 建立 SSE 连接。
	resp, err := http.Get(ts.URL + "/v1/events")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	br := newTimedReader(t, resp.Body)

	if line := br.readLine(); !strings.HasPrefix(line, "event: ready") {
		t.Fatalf("first SSE line = %q, want ready event", line)
	}

	// 提交一个请求并等待其走完超时发批流程。
	if _, body := postJSON(t, srv.httpSrv.Handler,
		`{"id":"sse-1","model":"m","prompt":"p"}`); body["status"] != "ok" {
		t.Fatalf("submit failed: %v", body)
	}

	// 读取流直到收集齐 opened/admitted/flushed/request_id 信号。
	var sawOpened, sawFlushed, sawAdmitted, sawID bool
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !(sawOpened && sawAdmitted && sawFlushed && sawID) {
		line := br.readLine()
		switch {
		case strings.Contains(line, `"batch.opened"`):
			sawOpened = true
		case strings.Contains(line, `"item.admitted"`):
			sawAdmitted = true
		case strings.Contains(line, `"batch.flushed"`):
			sawFlushed = true
		case strings.Contains(line, "sse-1"):
			sawID = true
		}
	}
	if !sawFlushed {
		t.Fatal("did not observe batch.flushed on SSE stream")
	}
	if !sawOpened || !sawAdmitted || !sawID {
		t.Fatalf("SSE missing opened=%v admitted=%v id=%v", sawOpened, sawAdmitted, sawID)
	}
}

// timedReader 给阻塞式 SSE 读取加上测试截止时间，失败即终止测试，
// 避免挂死整个测试进程。
type timedReader struct {
	t  *testing.T
	br *bufio.Reader
}

func newTimedReader(t *testing.T, r interface {
	Read(p []byte) (int, error)
}) *timedReader {
	return &timedReader{t: t, br: bufio.NewReader(r)}
}

func (r *timedReader) readLine() string {
	type result struct {
		line string
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		s, err := r.br.ReadString('\n')
		ch <- result{s, err}
	}()
	select {
	case x := <-ch:
		if x.err != nil {
			r.t.Fatalf("reading SSE: %v", x.err)
		}
		return x.line
	case <-time.After(2 * time.Second):
		r.t.Fatal("timeout reading SSE line")
		return ""
	}
}
