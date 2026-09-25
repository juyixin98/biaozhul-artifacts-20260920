// Command faultclient is the fault-injection client. It drives a running
// server through the acceptance scenarios — concurrent same-version
// updates, delete/recreate, wildcard and missing preconditions, weak
// ETags, and injected downstream failures — and prints structured JSON
// test results to stdout.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"
)

// Result is one structured scenario outcome.
type Result struct {
	Name       string `json:"name"`
	Passed     bool   `json:"passed"`
	DurationMs int64  `json:"durationMs"`
	Details    string `json:"details"`
}

type client struct {
	base string
	hc   *http.Client
}

func (c *client) do(method, path string, headers map[string]string, body string) (int, http.Header, []byte, error) {
	req, err := http.NewRequest(method, c.base+path, bytes.NewBufferString(body))
	if err != nil {
		return 0, nil, nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	return resp.StatusCode, resp.Header, b, err
}

func (c *client) create(id, body string) (string, error) {
	code, h, respBody, err := c.do(http.MethodPut, "/resources/"+id,
		map[string]string{"If-None-Match": "*"}, body)
	if err != nil {
		return "", err
	}
	if code != http.StatusOK {
		return "", fmt.Errorf("create %s: status %d: %s", id, code, respBody)
	}
	return h.Get("ETag"), nil
}

// cleanup removes any resource left behind by a previous run so the
// scenarios are idempotent and safe to re-run against the same server.
// If-Match: * deletes an existing resource; a missing one yields 412,
// which is the desired outcome here.
func (c *client) cleanup(ids ...string) {
	for _, id := range ids {
		code, _, _, _ := c.do(http.MethodDelete, "/resources/"+id,
			map[string]string{"If-Match": "*"}, "")
		_ = code // 204 (deleted) or 412 (already absent) — both fine
	}
}

func scenarioRace(c *client) (string, bool) {
	c.cleanup("sc-race")
	et, err := c.create("sc-race", "v1")
	if err != nil {
		return err.Error(), false
	}
	var wg sync.WaitGroup
	codes := make([]int, 2)
	start := make(chan struct{})
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			code, _, _, err := c.do(http.MethodPut, "/resources/sc-race",
				map[string]string{"If-Match": et}, fmt.Sprintf("racer-%d", i))
			if err != nil {
				codes[i] = -1
				return
			}
			codes[i] = code
		}(i)
	}
	close(start)
	wg.Wait()

	ok, failed := 0, 0
	for _, code := range codes {
		switch code {
		case http.StatusOK:
			ok++
		case http.StatusPreconditionFailed:
			failed++
		}
	}
	details := fmt.Sprintf("statuses=%v (want exactly one 200 and one 412)", codes)
	return details, ok == 1 && failed == 1
}

func scenarioDeleteRecreate(c *client) (string, bool) {
	c.cleanup("sc-delrec")
	et1, err := c.create("sc-delrec", "first")
	if err != nil {
		return err.Error(), false
	}
	code, _, _, err := c.do(http.MethodDelete, "/resources/sc-delrec",
		map[string]string{"If-Match": et1}, "")
	if err != nil || code != http.StatusNoContent {
		return fmt.Sprintf("delete: code=%d err=%v", code, err), false
	}
	et2, err := c.create("sc-delrec", "second")
	if err != nil {
		return err.Error(), false
	}
	if et1 == et2 {
		return "recreated resource reused old ETag", false
	}
	code, _, _, _ = c.do(http.MethodPut, "/resources/sc-delrec",
		map[string]string{"If-Match": et1}, "stale write")
	details := fmt.Sprintf("oldETag=%s newETag=%s staleWriteStatus=%d (want 412)", et1, et2, code)
	return details, code == http.StatusPreconditionFailed
}

func scenarioWildcard(c *client) (string, bool) {
	c.cleanup("sc-wild", "sc-wild-missing")
	if _, err := c.create("sc-wild", "v1"); err != nil {
		return err.Error(), false
	}
	codeOK, _, _, _ := c.do(http.MethodPut, "/resources/sc-wild",
		map[string]string{"If-Match": "*"}, "v2")
	codeMissing, _, _, _ := c.do(http.MethodPut, "/resources/sc-wild-missing",
		map[string]string{"If-Match": "*"}, "nope")
	details := fmt.Sprintf("existing=%d (want 200), missing=%d (want 412)", codeOK, codeMissing)
	return details, codeOK == http.StatusOK && codeMissing == http.StatusPreconditionFailed
}

func scenarioMissingPrecondition(c *client) (string, bool) {
	c.cleanup("sc-nopre")
	code, _, body, _ := c.do(http.MethodPut, "/resources/sc-nopre", nil, "no precondition")
	details := fmt.Sprintf("status=%d (want 428) body=%s", code, body)
	return details, code == http.StatusPreconditionRequired
}

func scenarioWeakETag(c *client) (string, bool) {
	c.cleanup("sc-weak")
	et, err := c.create("sc-weak", "v1")
	if err != nil {
		return err.Error(), false
	}
	code, _, _, _ := c.do(http.MethodPut, "/resources/sc-weak",
		map[string]string{"If-Match": "W/" + et}, "weak write")
	details := fmt.Sprintf("weak If-Match status=%d (want 412)", code)
	return details, code == http.StatusPreconditionFailed
}

func scenarioFaultNoSideEffects(c *client) (string, bool) {
	c.cleanup("sc-fault")
	et, err := c.create("sc-fault", "v1")
	if err != nil {
		return err.Error(), false
	}
	if code, _, body, _ := c.do(http.MethodPost, "/admin/faults", nil, `{"failNext":1}`); code != http.StatusOK {
		return fmt.Sprintf("set faults: %d %s", code, body), false
	}
	defer c.do(http.MethodPost, "/admin/faults", nil, `{"failNext":0,"latencyMs":0}`)

	code, _, _, _ := c.do(http.MethodPut, "/resources/sc-fault",
		map[string]string{"If-Match": et}, "must not land")
	if code != http.StatusServiceUnavailable {
		return fmt.Sprintf("injected failure status=%d (want 503)", code), false
	}
	_, h, _, _ := c.do(http.MethodGet, "/resources/sc-fault", nil, "")
	unchanged := h.Get("ETag") == et
	details := fmt.Sprintf("status=503, ETag after failed update=%s unchanged=%v", h.Get("ETag"), unchanged)
	return details, unchanged
}

func main() {
	base := flag.String("server", "http://127.0.0.1:8080", "base URL of the running server")
	flag.Parse()

	c := &client{base: *base, hc: &http.Client{Timeout: 10 * time.Second}}

	scenarios := []struct {
		name string
		fn   func(*client) (string, bool)
	}{
		{"concurrent-same-version-one-wins", scenarioRace},
		{"delete-recreate-stale-etag-rejected", scenarioDeleteRecreate},
		{"wildcard-if-match", scenarioWildcard},
		{"missing-precondition-428", scenarioMissingPrecondition},
		{"weak-etag-no-strong-match", scenarioWeakETag},
		{"fault-injection-no-side-effects", scenarioFaultNoSideEffects},
	}

	results := make([]Result, 0, len(scenarios))
	allPassed := true
	for _, sc := range scenarios {
		start := time.Now()
		details, passed := sc.fn(c)
		results = append(results, Result{
			Name:       sc.name,
			Passed:     passed,
			DurationMs: time.Since(start).Milliseconds(),
			Details:    details,
		})
		allPassed = allPassed && passed
	}

	summary := map[string]any{
		"server":  *base,
		"passed":  allPassed,
		"results": results,
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(summary)

	if !allPassed {
		os.Exit(1)
	}
}
