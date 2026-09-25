package httpapi

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"batchagg"
)

type testEnv struct {
	t       *testing.T
	srv     *Server
	ts      *httptest.Server
	sched   *batchagg.Scheduler
	rec     *batchagg.EventRecorder
	baseURL string
}

func newTestEnv(t *testing.T, cfg batchagg.Config) *testEnv {
	t.Helper()
	rec := batchagg.NewEventRecorder()
	broadcaster := NewEventBroadcaster(16)
	sinks := []batchagg.EventSink{rec, broadcaster}
	if cfg.Sink != nil {
		sinks = append([]batchagg.EventSink{cfg.Sink}, sinks...)
	}
	cfg.Sink = batchagg.MultiSink(sinks...)
	if cfg.MaxItems == 0 {
		cfg.MaxItems = 4
	}
	if cfg.MaxBytes == 0 {
		cfg.MaxBytes = 4096
	}
	if cfg.MaxWait == 0 {
		cfg.MaxWait = 60 * time.Millisecond
	}
	if cfg.Clock == nil {
		cfg.Clock = batchagg.NewSystemClock()
	}
	sched := batchagg.New(cfg, &batchagg.Simulator{DefaultModel: "sim-llm-1"})
	srv := NewServer(Config{
		Scheduler:    sched,
		MaxBodyBytes: cfg.MaxBytes + 1024,
		EventBuffer:  16,
		Broadcaster:  broadcaster,
	})
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(func() {
		ts.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
		_ = sched.Close()
	})
	return &testEnv{t: t, srv: srv, ts: ts, sched: sched, rec: rec, baseURL: ts.URL}
}

func (e *testEnv) infer(body string) (int, map[string]any, http.Header) {
	e.t.Helper()
	req, _ := http.NewRequest(http.MethodPost, e.baseURL+"/infer", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return resp.StatusCode, m, resp.Header
}

func TestHTTP_HealthAndRoot(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{})

	resp, err := http.Get(env.baseURL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health status %d", resp.StatusCode)
	}

	resp2, err := http.Get(env.baseURL + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("root status %d", resp2.StatusCode)
	}

	resp3, err := http.Get(env.baseURL + "/nope")
	if err != nil {
		t.Fatal(err)
	}
	defer resp3.Body.Close()
	if resp3.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown path status %d", resp3.StatusCode)
	}
}

func TestHTTP_BatchedRequestsSameBatchID(t *testing.T) {
	// MaxItems 3: three near-simultaneous requests should aggregate and share
	// one batch id, each with its own index and output.
	env := newTestEnv(t, batchagg.Config{MaxItems: 3, MaxWait: 2 * time.Second})

	var wg sync.WaitGroup
	batchIDs := make([]string, 3)
	indices := make([]float64, 3)
	statuses := make([]int, 3)
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			body := fmt.Sprintf(`{"model":"m1","prompt":"prompt-%d"}`, i)
			status, m, _ := env.infer(body)
			statuses[i] = status
			if status == http.StatusOK {
				batchIDs[i] = m["batch_id"].(string)
				indices[i] = m["index"].(float64)
				out := m["output"].(map[string]any)
				if out["echo"] != fmt.Sprintf("prompt-%d", i) {
					t.Errorf("item %d echo=%v", i, out["echo"])
				}
			}
		}(i)
	}
	wg.Wait()

	for i, st := range statuses {
		if st != http.StatusOK {
			t.Fatalf("request %d status %d", i, st)
		}
	}
	if batchIDs[0] != batchIDs[1] || batchIDs[1] != batchIDs[2] {
		t.Fatalf("batch ids differ: %v", batchIDs)
	}
	seen := map[float64]bool{}
	for i, idx := range indices {
		if idx < 0 || idx > 2 || seen[idx] {
			t.Fatalf("item %d index %v (all: %v)", i, idx, indices)
		}
		seen[idx] = true
	}
}

func TestHTTP_DifferentKeysDifferentBatches(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{MaxItems: 10, MaxWait: 80 * time.Millisecond})

	s1, m1, _ := env.infer(`{"model":"model-x","prompt":"a"}`)
	s2, m2, _ := env.infer(`{"model":"model-y","prompt":"b"}`)
	if s1 != http.StatusOK || s2 != http.StatusOK {
		t.Fatalf("statuses %d %d: %v %v", s1, s2, m1, m2)
	}
	if m1["batch_id"] == m2["batch_id"] {
		t.Fatalf("different compatibility keys shared batch %v", m1["batch_id"])
	}
}

func TestHTTP_ExplicitKeyOverridesModel(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{MaxItems: 2, MaxWait: 2 * time.Second})

	var wg sync.WaitGroup
	ids := make([]string, 2)
	for i, model := range []string{"model-a", "model-b"} {
		wg.Add(1)
		go func(i int, model string) {
			defer wg.Done()
			body := fmt.Sprintf(`{"key":"shared","model":%q,"prompt":"z"}`, model)
			status, m, _ := env.infer(body)
			if status != http.StatusOK {
				t.Errorf("status=%d body=%v", status, m)
				return
			}
			ids[i] = m["batch_id"].(string)
		}(i, model)
	}
	wg.Wait()
	if ids[0] != ids[1] {
		t.Fatalf("explicit key should aggregate: %v", ids)
	}
}

func TestHTTP_OversizeItemRejectedWith413(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{MaxBytes: 256, MaxWait: 100 * time.Millisecond})

	big := strings.Repeat("x", 300)
	status, m, _ := env.infer(fmt.Sprintf(`{"model":"m","prompt":%q}`, big))
	if status != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize status=%d body=%v", status, m)
	}
	if m["code"] != "oversize_item" {
		t.Fatalf("code=%v", m["code"])
	}

	// A normal request right after still succeeds independently.
	status, m, _ = env.infer(`{"model":"m","prompt":"small"}`)
	if status != http.StatusOK {
		t.Fatalf("normal request after oversize: status=%d body=%v", status, m)
	}
}

func TestHTTP_PartialFailureIsolated(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{MaxItems: 3, MaxWait: 2 * time.Second})

	type res struct {
		status int
		body   map[string]any
	}
	out := make([]res, 3)
	bodies := []string{
		`{"model":"m2","prompt":"good-a"}`,
		`{"model":"m2","prompt":"bad","fail":true,"failure_code":"BOOM"}`,
		`{"model":"m2","prompt":"good-b"}`,
	}
	var wg sync.WaitGroup
	for i, b := range bodies {
		wg.Add(1)
		go func(i int, b string) {
			defer wg.Done()
			st, m, _ := env.infer(b)
			out[i] = res{st, m}
		}(i, b)
	}
	wg.Wait()

	var sharedBatch string
	for i, r := range out {
		switch i {
		case 1:
			if r.status != http.StatusBadGateway {
				t.Fatalf("failed item status=%d body=%v", r.status, r.body)
			}
			if r.body["code"] != "simulated_inference_failure" ||
				!strings.Contains(r.body["error"].(string), "BOOM") {
				t.Fatalf("failed item body=%v", r.body)
			}
		default:
			if r.status != http.StatusOK {
				t.Fatalf("sibling %d status=%d body=%v", i, r.status, r.body)
			}
			bid := r.body["batch_id"].(string)
			if sharedBatch == "" {
				sharedBatch = bid
			} else if bid != sharedBatch {
				t.Fatalf("sibling %d not in shared batch: %s vs %s", i, bid, sharedBatch)
			}
		}
	}
	// The failed item should have shared the same batch as its siblings.
	// (batch_id isn't returned on error, so verify via recorder events.)
	results := env.rec.OfType(batchagg.EventItemResult)
	var failBatch string
	var okBatches []string
	for _, ev := range results {
		if strings.HasPrefix(ev.ItemID, "req-") {
			if ev.Success {
				okBatches = append(okBatches, ev.BatchID)
			} else if failBatch == "" {
				failBatch = ev.BatchID
			}
		}
	}
	if failBatch == "" || len(okBatches) != 2 || okBatches[0] != failBatch || okBatches[1] != failBatch {
		t.Fatalf("events did not show 2 successes + 1 failure in one batch: ok=%v fail=%s", okBatches, failBatch)
	}
}

func TestHTTP_BadJSONAndMissingKey(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{})

	status, m, _ := env.infer("{not json")
	if status != http.StatusBadRequest || m["code"] != "bad_json" {
		t.Fatalf("bad json: %d %v", status, m)
	}
	status, m, _ = env.infer(`{"prompt":"no key"}`)
	if status != http.StatusBadRequest || m["code"] != "missing_key" {
		t.Fatalf("missing key: %d %v", status, m)
	}
}

func TestHTTP_EventsStreamDeliversStructuredEvents(t *testing.T) {
	env := newTestEnv(t, batchagg.Config{MaxItems: 10, MaxWait: 120 * time.Millisecond})

	resp, err := http.Get(env.baseURL + "/events")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK || !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("events status=%d ct=%s", resp.StatusCode, resp.Header.Get("Content-Type"))
	}

	// Give the subscriber a moment to register, then generate traffic.
	time.Sleep(50 * time.Millisecond)
	if _, m, _ := env.infer(`{"model":"stream-model","prompt":"hello events"}`); m == nil {
		t.Fatal("request failed")
	}

	br := bufio.NewReader(resp.Body)
	wantTypes := map[string]bool{
		"batch_open": false, "submitted": false, "batch_flush": false, "item_result": false,
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "event: ") {
			typ := strings.TrimPrefix(line, "event: ")
			if _, ok := wantTypes[typ]; ok {
				wantTypes[typ] = true
			}
		}
		all := true
		for _, seen := range wantTypes {
			if !seen {
				all = false
			}
		}
		if all {
			break
		}
	}
	for typ, seen := range wantTypes {
		if !seen {
			t.Fatalf("SSE stream never delivered %q", typ)
		}
	}
}

func TestHTTP_RequestBodyTooLargeHTTPLayer(t *testing.T) {
	// Server HTTP cap (256) below scheduler limit (4096): the HTTP layer
	// rejects before the scheduler even sees the item.
	rec := batchagg.NewEventRecorder()
	sched := batchagg.New(batchagg.Config{
		MaxItems: 10, MaxBytes: 4096, MaxWait: 100 * time.Millisecond, Sink: rec,
	}, &batchagg.Simulator{})
	srv := NewServer(Config{Scheduler: sched, MaxBodyBytes: 256})
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	defer sched.Close()

	resp, err := http.Post(ts.URL+"/infer", "application/json",
		bytes.NewReader([]byte(fmt.Sprintf(`{"model":"m","prompt":%q}`, strings.Repeat("y", 500)))))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	var m map[string]any
	raw, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(raw, &m)
	if m["code"] != "http_body_too_large" {
		t.Fatalf("code=%v", m["code"])
	}
}
