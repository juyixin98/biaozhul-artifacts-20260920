// Command faultclient is the fault-injection client for the multi-tenant
// compute service. It drives the acceptance scenarios — cross-tenant
// same-name cache keys, single-tenant overload, cancellation, and tenant
// override rejection — and emits a structured JSON test report. It talks
// only to the local server; no production system is involved.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"
)

// Step is one assertion inside a scenario.
type Step struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Expect string `json:"expect"`
	Got    string `json:"got"`
}

// Scenario is a named group of steps.
type Scenario struct {
	Name       string `json:"name"`
	OK         bool   `json:"ok"`
	DurationMs int64  `json:"duration_ms"`
	Steps      []Step `json:"steps"`
}

// Report is the structured test result emitted as JSON.
type Report struct {
	StartedAt time.Time  `json:"started_at"`
	Target    string     `json:"target"`
	Scenarios []Scenario `json:"scenarios"`
	Passed    int        `json:"passed"`
	Failed    int        `json:"failed"`
	OK        bool       `json:"ok"`
}

type client struct {
	base string
	hc   *http.Client
}

func (c *client) do(ctx context.Context, method, path, tenant string, body []byte) (int, map[string]any, error) {
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rdr)
	if err != nil {
		return 0, nil, err
	}
	if tenant != "" {
		req.Header.Set("X-Tenant-ID", tenant)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var parsed map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &parsed)
	}
	return resp.StatusCode, parsed, nil
}

func (c *client) get(path, tenant string) (int, map[string]any, error) {
	return c.do(context.Background(), "GET", path, tenant, nil)
}

func (c *client) post(path, tenant string, body []byte) (int, map[string]any, error) {
	return c.do(context.Background(), "POST", path, tenant, body)
}

func step(name, expect string, ok bool, gotFmt string, args ...any) Step {
	return Step{Name: name, Expect: expect, OK: ok, Got: fmt.Sprintf(gotFmt, args...)}
}

func main() {
	var (
		addr   = flag.String("addr", "http://127.0.0.1:8080", "server base URL")
		report = flag.String("report", "", "write JSON report to this file (default: stdout only)")
	)
	flag.Parse()

	c := &client{base: *addr, hc: &http.Client{Timeout: 45 * time.Second}}
	rep := Report{StartedAt: time.Now(), Target: *addr}

	scenarios := []func(*client) Scenario{
		scenarioCrossTenantSameKey,
		scenarioTenantOverrideRejected,
		scenarioSingleTenantOverload,
		scenarioCancellation,
	}
	for _, fn := range scenarios {
		sc := fn(c)
		rep.Scenarios = append(rep.Scenarios, sc)
		if sc.OK {
			rep.Passed++
		} else {
			rep.Failed++
		}
	}
	rep.OK = rep.Failed == 0

	out, _ := json.MarshalIndent(rep, "", "  ")
	fmt.Println(string(out))
	if *report != "" {
		if err := os.WriteFile(*report, out, 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "write report: %v\n", err)
			os.Exit(2)
		}
	}
	if !rep.OK {
		os.Exit(1)
	}
}

func finish(name string, start time.Time, steps []Step) Scenario {
	ok := true
	for _, s := range steps {
		if !s.OK {
			ok = false
			break
		}
	}
	return Scenario{Name: name, OK: ok, DurationMs: time.Since(start).Milliseconds(), Steps: steps}
}

// scenarioCrossTenantSameKey proves two tenants can use the identical
// logical key without observing each other's data.
func scenarioCrossTenantSameKey(c *client) Scenario {
	start := time.Now()
	var steps []Step

	codeA, jobA, err := c.post("/v1/compute?wait=true", "tenant-a",
		[]byte(`{"key":"shared-key","work":100000,"mem_bytes":4096}`))
	steps = append(steps, step("tenant-a compute completes", "200 completed",
		err == nil && codeA == 200 && jobA["status"] == "completed",
		"code=%d status=%v err=%v", codeA, jobA["status"], err))

	codeB, jobB, err := c.post("/v1/compute?wait=true", "tenant-b",
		[]byte(`{"key":"shared-key","work":200000,"mem_bytes":4096}`))
	steps = append(steps, step("tenant-b compute completes", "200 completed",
		err == nil && codeB == 200 && jobB["status"] == "completed",
		"code=%d status=%v err=%v", codeB, jobB["status"], err))

	resA, _ := jobA["result"].(string)
	resB, _ := jobB["result"].(string)
	steps = append(steps, step("results differ for different work", "result-a != result-b",
		resA != "" && resB != "" && resA != resB, "a=%s b=%s", resA, resB))

	keyA, _ := jobA["cache_key"].(string)
	keyB, _ := jobB["cache_key"].(string)
	steps = append(steps, step("cache keys are tenant-scoped", "compute|tenant-a|... != compute|tenant-b|...",
		keyA == "compute|tenant-a|shared-key" && keyB == "compute|tenant-b|shared-key",
		"a=%s b=%s", keyA, keyB))

	_, gotA, errA := c.get("/v1/cache/shared-key", "tenant-a")
	_, gotB, errB := c.get("/v1/cache/shared-key", "tenant-b")
	steps = append(steps, step("each tenant reads back only its own value", "cache[a]==result-a && cache[b]==result-b",
		errA == nil && errB == nil && gotA["value"] == resA && gotB["value"] == resB,
		"cacheA=%v cacheB=%v", gotA["value"], gotB["value"]))

	return finish("cross_tenant_same_key_isolation", start, steps)
}

// scenarioTenantOverrideRejected proves the body cannot smuggle identity.
func scenarioTenantOverrideRejected(c *client) Scenario {
	start := time.Now()
	var steps []Step

	code, body, err := c.post("/v1/compute", "tenant-a",
		[]byte(`{"key":"k","work":1000,"tenant_id":"tenant-b"}`))
	errCode, _ := body["error"].(map[string]any)["code"].(string)
	steps = append(steps, step("tenant_id in body rejected", "400 TENANT_OVERRIDE_REJECTED",
		err == nil && code == 400 && errCode == "TENANT_OVERRIDE_REJECTED",
		"code=%d errCode=%s err=%v", code, errCode, err))

	code, _, err = c.post("/v1/compute", "tenant-a",
		[]byte(`{"key":"k","work":1000,"tenantId":"tenant-b"}`))
	steps = append(steps, step("tenantId (camelCase) in body rejected", "400",
		err == nil && code == 400, "code=%d err=%v", code, err))

	code, _, err = c.post("/v1/compute", "", []byte(`{"key":"k","work":1000}`))
	steps = append(steps, step("missing auth header rejected", "401", err == nil && code == 401,
		"code=%d err=%v", code, err))

	return finish("tenant_override_rejected", start, steps)
}

// scenarioSingleTenantOverload floods tenant-heavy past its queue depth and
// verifies (a) it gets 429s and (b) tenant-quiet is still served.
func scenarioSingleTenantOverload(c *client) Scenario {
	start := time.Now()
	var steps []Step

	const flood = 16
	type res struct {
		code int
		err  error
	}
	results := make([]res, flood)
	var wg sync.WaitGroup
	for i := 0; i < flood; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			code, _, err := c.post("/v1/compute", "tenant-heavy",
				[]byte(`{"key":"flood","work":20000000,"mem_bytes":1048576}`))
			results[i] = res{code: code, err: err}
		}(i)
	}
	wg.Wait()

	accepted, rejected, errs := 0, 0, 0
	for _, r := range results {
		switch {
		case r.err != nil:
			errs++
		case r.code == http.StatusTooManyRequests:
			rejected++
		case r.code == http.StatusAccepted:
			accepted++
		}
	}
	steps = append(steps, step("overloaded tenant gets 429s", "rejected > 0, no transport errors",
		rejected > 0 && errs == 0, "accepted=%d rejected=%d errs=%d", accepted, rejected, errs))
	// Admissions are bounded: at any instant the tenant holds at most
	// queue-depth queued + in-flight running jobs, so a flood larger than
	// that must be partially rejected. Workers draining the queue during
	// the flood can free slots, so the exact accept count is timing
	// dependent; the invariant is that not everything was admitted.
	steps = append(steps, step("admissions bounded by queue backpressure", "0 < accepted < flood",
		accepted > 0 && accepted < flood, "accepted=%d flood=%d", accepted, flood))

	quietStart := time.Now()
	code, job, err := c.post("/v1/compute?wait=true&timeout_ms=20000", "tenant-quiet",
		[]byte(`{"key":"quiet","work":100000,"mem_bytes":4096}`))
	quietElapsed := time.Since(quietStart)
	steps = append(steps, step("other tenant still served during overload", "200 completed",
		err == nil && code == 200 && job["status"] == "completed",
		"code=%d status=%v elapsed=%s err=%v", code, job["status"], quietElapsed.Round(time.Millisecond), err))

	_, stats, err := c.get("/v1/stats", "tenant-heavy")
	st, _ := stats["stats"].(map[string]any)
	rej, _ := st["rejected_queue_full"].(float64)
	steps = append(steps, step("stats record queue-full rejections", "rejected_queue_full > 0",
		err == nil && rej > 0, "rejected_queue_full=%v err=%v", rej, err))

	return finish("single_tenant_overload", start, steps)
}

// scenarioCancellation covers explicit cancel, cross-tenant cancel refusal,
// and client-disconnect cancellation of a waited request.
func scenarioCancellation(c *client) Scenario {
	start := time.Now()
	var steps []Step

	code, job, err := c.post("/v1/compute", "tenant-a",
		[]byte(`{"key":"long","work":900000000,"mem_bytes":4096}`))
	jobID, _ := job["job_id"].(string)
	steps = append(steps, step("long job accepted", "202 with job_id",
		err == nil && code == 202 && jobID != "", "code=%d id=%s err=%v", code, jobID, err))

	code, _, err = c.do(context.Background(), "DELETE", "/v1/jobs/"+jobID, "tenant-b", nil)
	steps = append(steps, step("cross-tenant cancel refused", "404", err == nil && code == 404,
		"code=%d err=%v", code, err))

	code, _, err = c.do(context.Background(), "DELETE", "/v1/jobs/"+jobID, "tenant-a", nil)
	steps = append(steps, step("owner cancel accepted", "200", err == nil && code == 200,
		"code=%d err=%v", code, err))

	finalStatus := ""
	for i := 0; i < 100; i++ {
		_, snap, err := c.get("/v1/jobs/"+jobID, "tenant-a")
		if err == nil {
			finalStatus, _ = snap["status"].(string)
			if finalStatus == "cancelled" {
				break
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	steps = append(steps, step("job reaches cancelled state", "cancelled",
		finalStatus == "cancelled", "status=%s", finalStatus))

	// Client-disconnect cancellation: start a waited request, abort it from
	// the client side, and confirm the server cancels the underlying job.
	_, statsBefore, _ := c.get("/v1/stats", "tenant-disc")
	cancelledBefore := statsValue(statsBefore, "cancelled")

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _, _ = c.do(ctx, "POST", "/v1/compute?wait=true&timeout_ms=30000", "tenant-disc",
			[]byte(`{"key":"disconnect","work":900000000,"mem_bytes":4096}`))
	}()
	time.Sleep(500 * time.Millisecond) // let the server start the job
	cancel()                           // simulate the client going away
	<-done

	cancelledAfter := cancelledBefore
	for i := 0; i < 100; i++ {
		_, statsNow, err := c.get("/v1/stats", "tenant-disc")
		if err == nil {
			cancelledAfter = statsValue(statsNow, "cancelled")
			if cancelledAfter > cancelledBefore {
				break
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	steps = append(steps, step("client disconnect cancels the job", "cancelled count increases",
		cancelledAfter > cancelledBefore, "before=%v after=%v", cancelledBefore, cancelledAfter))

	return finish("cancellation", start, steps)
}

func statsValue(stats map[string]any, field string) float64 {
	st, _ := stats["stats"].(map[string]any)
	v, _ := st[field].(float64)
	return v
}
