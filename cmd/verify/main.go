// Command verify runs the acceptance scenarios for the conditional-update
// service entirely in-process (httptest server, fake clock, fake downstream)
// and emits a structured JSON report. Exit code is 0 only when every scenario
// passes.
//
// Usage:
//
//	go run ./cmd/verify [-out report.json]
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http/httptest"
	"os"
	"sync"
	"time"

	"etagrace/client"
	"etagrace/internal/clock"
	"etagrace/internal/notifier"
	"etagrace/internal/server"
	"etagrace/internal/store"
)

type harness struct {
	clk    *clock.Fake
	audit  *notifier.AuditLog
	faulty *notifier.FaultyNotifier
	httpd  *httptest.Server
	cli    *client.Client
}

func newHarness() *harness {
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	audit := notifier.NewAuditLog()
	faulty := notifier.NewFaulty(audit, clk)
	retrying := server.RetryingNotifier{
		Inner: faulty, Clk: clk, Attempts: 3,
		BaseBackoff: time.Millisecond, MaxBackoff: 4 * time.Millisecond,
	}
	st := store.New(clk)
	srv := server.New(st, clk, retrying, audit)
	httpd := httptest.NewServer(srv.Handler())
	cli := client.New(httpd.URL, httpd.Client(), client.NewFailurePolicy(0, 0, clk.Sleep))
	return &harness{clk: clk, audit: audit, faulty: faulty, httpd: httpd, cli: cli}
}

func (h *harness) close() { h.httpd.Close() }

// assertion accumulators ----------------------------------------------------

type assertion struct {
	Name   string `json:"name"`
	Passed bool   `json:"passed"`
	Detail string `json:"detail,omitempty"`
}

type scenario struct {
	Name       string      `json:"name"`
	Passed     bool        `json:"passed"`
	Assertions []assertion `json:"assertions"`
}

type report struct {
	GeneratedAt string     `json:"generatedAt"`
	Scenarios   []scenario `json:"scenarios"`
	Total       int        `json:"total"`
	Passed      int        `json:"passed"`
	Failed      int        `json:"failed"`
}

type checker struct {
	scn scenario
}

func (c *checker) check(name string, ok bool, detail string) {
	c.scn.Assertions = append(c.scn.Assertions, assertion{Name: name, Passed: ok, Detail: detail})
}

func (c *checker) finish() scenario {
	for _, a := range c.scn.Assertions {
		if !a.Passed {
			return c.scn
		}
	}
	c.scn.Passed = true
	return c.scn
}

// scenarios -----------------------------------------------------------------

func raceScenario(h *harness) scenario {
	c := &checker{scn: scenario{Name: "concurrent_same_version_only_one_wins"}}
	ctx := context.Background()

	created, err := h.cli.Create(ctx, "doc", []byte("v0"))
	must(c, "create initial resource", err)
	tag := created.ETag

	type outcome struct{ status int }
	outcomes := make([]outcome, 2)
	auditBefore := h.audit.Len()
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i, body := range [][]byte{[]byte("alice"), []byte("bob")} {
		wg.Add(1)
		go func(i int, body []byte) {
			defer wg.Done()
			<-start
			res, _ := h.cli.Update(ctx, "doc", tag, body)
			outcomes[i] = outcome{res.StatusCode}
		}(i, body)
	}
	close(start)
	wg.Wait()

	wins, losses := 0, 0
	for _, o := range outcomes {
		switch o.status {
		case 200:
			wins++
		case 412:
			losses++
		}
	}
	c.check("exactly one update succeeded (200)", wins == 1, fmt.Sprintf("200s=%d", wins))
	c.check("exactly one update lost the race (412)", losses == 1, fmt.Sprintf("412s=%d", losses))

	got, _ := h.cli.Get(ctx, "doc")
	ver, _ := client.VersionFromTag(got.ETag)
	c.check("final version advanced exactly once (v2)", ver == 2, fmt.Sprintf("final=%s", got.ETag))
	c.check("final content is one whole writer body",
		string(got.Body) == "alice" || string(got.Body) == "bob", fmt.Sprintf("body=%q", got.Body))
	c.check("exactly one audit side effect recorded",
		h.audit.Len()-auditBefore == 1, fmt.Sprintf("new audit events=%d", h.audit.Len()-auditBefore))
	return c.finish()
}

func deleteRecreateScenario(h *harness) scenario {
	c := &checker{scn: scenario{Name: "delete_and_recreate_never_reuses_version"}}
	ctx := context.Background()

	created, err := h.cli.Create(ctx, "k", []byte("first"))
	must(c, "create v1", err)
	oldTag := created.ETag

	del, err := h.cli.Delete(ctx, "k", oldTag)
	must(c, "delete with matching tag -> 204", err)
	c.check("delete status 204", del.StatusCode == 204, fmt.Sprintf("status=%d", del.StatusCode))

	stale, _ := h.cli.Update(ctx, "k", oldTag, []byte("ghost"))
	c.check("PUT old tag after delete -> 412", stale.StatusCode == 412, fmt.Sprintf("status=%d", stale.StatusCode))

	recreated, err := h.cli.Create(ctx, "k", []byte("second"))
	must(c, "recreate resource", err)
	newTag := recreated.ETag
	newVer, _ := client.VersionFromTag(newTag)
	c.check("recreated version is strictly higher (v3)", newVer == 3, fmt.Sprintf("new=%s", newTag))
	c.check("recreated ETag differs from deleted one", newTag != oldTag,
		fmt.Sprintf("old=%s new=%s", oldTag, newTag))

	oldAgain, _ := h.cli.Update(ctx, "k", oldTag, []byte("ghost"))
	c.check("PUT deleted tag against live resource -> 412",
		oldAgain.StatusCode == 412, fmt.Sprintf("status=%d", oldAgain.StatusCode))

	fresh, _ := h.cli.Update(ctx, "k", newTag, []byte("third"))
	c.check("PUT current tag -> 200", fresh.StatusCode == 200, fmt.Sprintf("status=%d", fresh.StatusCode))
	return c.finish()
}

func wildcardScenario(h *harness) scenario {
	c := &checker{scn: scenario{Name: "wildcard_if_match_star"}}
	ctx := context.Background()

	_, err := h.cli.Create(ctx, "w", []byte("a"))
	must(c, "create", err)

	ok, _ := h.cli.Update(ctx, "w", "*", []byte("b"))
	c.check("PUT If-Match:* on existing -> 200", ok.StatusCode == 200, fmt.Sprintf("status=%d", ok.StatusCode))

	del, _ := h.cli.Delete(ctx, "w", "*")
	c.check("DELETE If-Match:* -> 204", del.StatusCode == 204, fmt.Sprintf("status=%d", del.StatusCode))

	gone, _ := h.cli.Update(ctx, "w", "*", []byte("c"))
	c.check("PUT If-Match:* with no representation -> 412",
		gone.StatusCode == 412, fmt.Sprintf("status=%d", gone.StatusCode))
	return c.finish()
}

func missingPreconditionScenario(h *harness) scenario {
	c := &checker{scn: scenario{Name: "missing_precondition_is_428"}}
	ctx := context.Background()

	created, err := h.cli.Create(ctx, "m", []byte("keep"))
	must(c, "create", err)

	put, _ := h.cli.Update(ctx, "m", "", []byte("changed"))
	del, _ := h.cli.Delete(ctx, "m", "")
	c.check("PUT without If-Match -> 428", put.StatusCode == 428, fmt.Sprintf("status=%d", put.StatusCode))
	c.check("DELETE without If-Match -> 428", del.StatusCode == 428, fmt.Sprintf("status=%d", del.StatusCode))

	got, _ := h.cli.Get(ctx, "m")
	c.check("resource untouched after rejected requests",
		got.StatusCode == 200 && got.ETag == created.ETag && string(got.Body) == "keep",
		fmt.Sprintf("tag=%s body=%q", got.ETag, got.Body))
	return c.finish()
}

func weakTagScenario(h *harness) scenario {
	c := &checker{scn: scenario{Name: "weak_etag_cannot_strong_compare"}}
	ctx := context.Background()

	created, err := h.cli.Create(ctx, "e", []byte("same"))
	must(c, "create", err)
	weak := "W/" + created.ETag // same opaque value, weak marking

	res, _ := h.cli.Update(ctx, "e", weak, []byte("same"))
	c.check("weak If-Match -> 412", res.StatusCode == 412, fmt.Sprintf("status=%d", res.StatusCode))
	c.check("rejected specifically as weak", res.Outcome == "weak_etag_rejected",
		"outcome="+res.Outcome)

	got, _ := h.cli.Get(ctx, "e")
	c.check("strong ETag unchanged (no side effect)", got.ETag == created.ETag, got.ETag)
	return c.finish()
}

func noSideEffectsScenario(h *harness) scenario {
	c := &checker{scn: scenario{Name: "downstream_failure_leaves_no_side_effect"}}
	ctx := context.Background()

	created, err := h.cli.Create(ctx, "s", []byte("orig"))
	must(c, "create", err)
	auditBefore := h.audit.Len()

	h.faulty.ArmFailures(-1) // fail every notification
	failed, _ := h.cli.Update(ctx, "s", created.ETag, []byte("should-not-land"))
	h.faulty.Disarm()

	c.check("server reports 503 after retries exhausted", failed.StatusCode == 503,
		fmt.Sprintf("status=%d", failed.StatusCode))
	c.check("no audit event written on failure", h.audit.Len() == auditBefore,
		fmt.Sprintf("audit delta=%d", h.audit.Len()-auditBefore))

	got, _ := h.cli.Get(ctx, "s")
	c.check("version/content/ETag unchanged",
		got.ETag == created.ETag && string(got.Body) == "orig",
		fmt.Sprintf("tag=%s body=%q", got.ETag, got.Body))

	recovered, _ := h.cli.Update(ctx, "s", created.ETag, []byte("lands-now"))
	c.check("same tag works once downstream recovers -> 200",
		recovered.StatusCode == 200, fmt.Sprintf("status=%d", recovered.StatusCode))
	c.check("audit event written after recovery", h.audit.Len() == auditBefore+1,
		fmt.Sprintf("audit delta=%d", h.audit.Len()-auditBefore))
	return c.finish()
}

func must(c *checker, name string, err error) {
	c.check(name, err == nil, errString(err))
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func main() {
	out := flag.String("out", "", "write JSON report to this path (default: stdout)")
	flag.Parse()

	h := newHarness()
	defer h.close()

	scenarios := []func(*harness) scenario{
		raceScenario,
		deleteRecreateScenario,
		noSideEffectsScenario,
		wildcardScenario,
		missingPreconditionScenario,
		weakTagScenario,
	}
	rep := report{GeneratedAt: h.clk.Now().UTC().Format(time.RFC3339)}
	for _, run := range scenarios {
		s := run(h)
		rep.Scenarios = append(rep.Scenarios, s)
		rep.Total++
		if s.Passed {
			rep.Passed++
		} else {
			rep.Failed++
		}
	}

	data, _ := json.MarshalIndent(rep, "", "  ")
	if *out != "" {
		if err := os.WriteFile(*out, data, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, "write report:", err)
			os.Exit(2)
		}
	}
	fmt.Println(string(data))
	fmt.Fprintf(os.Stderr, "verify: %d/%d scenarios passed\n", rep.Passed, rep.Total)
	if rep.Failed > 0 {
		os.Exit(1)
	}
}
