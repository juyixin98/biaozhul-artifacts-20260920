// Command acceptance drives the running decision server end-to-end over real
// HTTP, exercising the four controlled scenarios and asserting every outcome.
// It is the "acceptance command" referenced by the README.
//
// Usage:
//
//	go run ./cmd/acceptance -url http://localhost:8080 \
//	    -key local-dev-key -secret dev-shared-secret-change-me
//
// Exit code 0 means every assertion passed; 1 means at least one failed.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/example/rollout/internal/signclient"
)

type report struct {
	failed int
	step   int
}

func (r *report) ok(cond bool, title, detail string) {
	r.step++
	mark := "PASS"
	if !cond {
		mark = "FAIL"
		r.failed++
	}
	fmt.Printf("  [%s] %d. %s\n", mark, r.step, title)
	if detail != "" {
		for _, line := range strings.Split(strings.TrimSpace(detail), "\n") {
			fmt.Printf("        %s\n", line)
		}
	}
}

func main() {
	base := flag.String("url", "http://localhost:8080", "decision server base URL")
	key := flag.String("key", "local-dev-key", "HMAC key id")
	secret := flag.String("secret", "dev-shared-secret-change-me", "HMAC secret")
	obsMS := flag.Int("obs-ms", 1200, "observation window in ms (must be a multiple of 200)")
	minSamples := flag.Int("min-samples", 80, "minimum samples per stage")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	c := signclient.New(*base, *key, *secret)
	rep := &report{}

	// Wait for the server to be reachable.
	if err := waitReady(ctx, c); err != nil {
		fmt.Printf("FATAL: server not reachable at %s: %v\n", *base, err)
		os.Exit(1)
	}
	fmt.Println("== Progressive rollout acceptance ==")

	scenarioHealthy(ctx, c, rep, *obsMS, *minSamples)
	scenarioSpike(ctx, c, rep, *obsMS, *minSamples)
	scenarioDegraded(ctx, c, rep, *obsMS, *minSamples)
	scenarioInsufficient(ctx, c, rep, *obsMS, *minSamples)
	scenarioBlackout(ctx, c, rep, *obsMS, *minSamples)
	scenarioRollbackLate(ctx, c, rep, *obsMS, *minSamples)
	scenarioFreezeAndGeneration(ctx, c, rep, *obsMS, *minSamples)

	fmt.Println()
	if rep.failed == 0 {
		fmt.Printf("ALL ACCEPTANCE CHECKS PASSED (%d steps)\n", rep.step)
		return
	}
	fmt.Printf("%d OF %d CHECKS FAILED\n", rep.failed, rep.step)
	os.Exit(1)
}

func waitReady(ctx context.Context, c *signclient.Client) error {
	deadline := time.Now().Add(15 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		resp, _, err := c.Get(ctx, "/healthz")
		if err == nil && resp.StatusCode == 200 {
			return nil
		}
		lastErr = err
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
	return lastErr
}

// ---- low level helpers ----

func createRelease(ctx context.Context, c *signclient.Client, scenario string, obsMS, minSamples int) map[string]any {
	body := map[string]any{
		"name": "acc-" + scenario, "version": "v" + fmt.Sprint(time.Now().UnixNano()),
		"scenario": scenario, "observation_ms": obsMS, "min_samples": minSamples,
	}
	b, _ := json.Marshal(body)
	resp, raw, err := c.Do(ctx, "POST", "/api/releases", b, "acc-create-"+scenario+"-"+fmt.Sprint(time.Now().UnixNano()))
	if err != nil {
		fatalf("create release: %v", err)
	}
	if resp.StatusCode != 201 {
		fatalf("create release status=%d body=%s", resp.StatusCode, raw)
	}
	return decode(raw)
}

func command(ctx context.Context, c *signclient.Client, id, cmd string, gen int64) (int, map[string]any) {
	body, _ := json.Marshal(map[string]any{"expected_generation": gen})
	resp, raw, err := c.Do(ctx, "POST",
		fmt.Sprintf("/api/releases/%s/commands/%s", id, cmd), body, "")
	if err != nil {
		fatalf("command %s: %v", cmd, err)
	}
	return resp.StatusCode, decode(raw)
}

func evaluate(ctx context.Context, c *signclient.Client, id string) map[string]any {
	resp, raw, err := c.Get(ctx, "/api/releases/"+id+"/evaluate")
	if err != nil || resp.StatusCode != 200 {
		fatalf("evaluate: %v status=%d body=%s", err, resp.StatusCode, raw)
	}
	return decode(raw)
}

func release(ctx context.Context, c *signclient.Client, id string) map[string]any {
	resp, raw, err := c.Get(ctx, "/api/releases/"+id)
	if err != nil || resp.StatusCode != 200 {
		fatalf("get release: %v status=%d", err, resp.StatusCode)
	}
	return decode(raw)
}

func verdictOf(ev map[string]any) map[string]any {
	return ev["observation"].(map[string]any)["Verdict"].(map[string]any)
}

func metricsOf(v map[string]any) map[string]any {
	return v["metrics"].(map[string]any)
}

func evidenceLine(v map[string]any) string {
	m := metricsOf(v)
	return fmt.Sprintf(
		"health=%s reasons=%v samples=%v errors=%v buckets=%v/%v\n"+
			"        errorRate95%%=[%v,%v] latencyMean95%%=[%v,%v]ms threshold=v%v",
		v["health"], v["reasons"], m["samples"], m["errors"],
		m["buckets_covered"], m["buckets_required"],
		m["error_rate_lower"], m["error_rate_upper"],
		m["latency_lower_ms"], m["latency_upper_ms"], v["threshold_version"])
}

// ---- scenarios ----

// 1) Healthy: walk 5% -> 20% and stop.
func scenarioHealthy(ctx context.Context, c *signclient.Client, rep *report, obsMS, minSamples int) {
	fmt.Println("\n[scenario: healthy — full window, healthy metrics]")
	r := createRelease(ctx, c, "healthy", obsMS, minSamples)
	id, _ := r["id"].(string)

	early := verdictOf(evaluate(ctx, c, id))
	rep.ok(early["health"] == "unknown",
		"before window completes verdict is unknown (never presumed healthy)",
		evidenceLine(early))

	time.Sleep(time.Duration(obsMS+250) * time.Millisecond)

	good := verdictOf(evaluate(ctx, c, id))
	rep.ok(good["health"] == "healthy",
		"after a full healthy window verdict is healthy with interval evidence",
		evidenceLine(good))

	st, out := command(ctx, c, id, "advance", gen(r))
	rep.ok(st == 200 && out["applied"] == true,
		"advance 5%->20% accepted at generation 0",
		fmt.Sprintf("status=%d stage=%v gen=%v", st, out["release"].(map[string]any)["stage"], out["release"].(map[string]any)["generation"]))
}

// 2) Spike: a burst that starts at stage entry and is fully contained in a
// short observation window is flagged degraded on interval evidence.
func scenarioSpike(ctx context.Context, c *signclient.Client, rep *report, obsMS, _ int) {
	fmt.Println("\n[scenario: spike-short — burst fully inside the first window]")
	r := createRelease(ctx, c, "spike-short", 800, 80)
	id, _ := r["id"].(string)
	// Wait just past the 800ms window so it is complete and fully covered by
	// the elapsed 0..1.2s burst.
	time.Sleep(1050 * time.Millisecond)
	v := verdictOf(evaluate(ctx, c, id))
	rep.ok(v["health"] == "degraded",
		"a burst contained in the window is flagged degraded",
		evidenceLine(v))
}

// 3) Degraded: persistent regression blocks advance; rollback is permitted.
func scenarioDegraded(ctx context.Context, c *signclient.Client, rep *report, obsMS, minSamples int) {
	fmt.Println("\n[scenario: degraded — persistent regression]")
	r := createRelease(ctx, c, "degraded", obsMS, minSamples)
	id, _ := r["id"].(string)
	time.Sleep(time.Duration(obsMS+250) * time.Millisecond)

	v := verdictOf(evaluate(ctx, c, id))
	rep.ok(v["health"] == "degraded",
		"persistent bad metrics produce a degraded verdict",
		evidenceLine(v))

	st, out := command(ctx, c, id, "advance", gen(r))
	rej, _ := out["rejection"].(map[string]any)
	rep.ok(st == 409 && out["applied"] == false && rej != nil &&
		rej["verdict"].(map[string]any)["health"] == "degraded",
		"advance is rejected (409) and the rejection carries degraded evidence",
		fmt.Sprintf("status=%d applied=%v", st, out["applied"]))

	st, out = command(ctx, c, id, "rollback", gen(r))
	rep.ok(st == 200 && out["applied"] == true &&
		out["release"].(map[string]any)["state"] == "rolled_back",
		"rollback is allowed and moves the release to rolled_back",
		fmt.Sprintf("status=%d state=%v", st, out["release"].(map[string]any)["state"]))
}

// 4) Insufficient samples: window complete and buckets present, but traffic is
// below the minimum — unknown, not healthy.
func scenarioInsufficient(ctx context.Context, c *signclient.Client, rep *report, obsMS, minSamples int) {
	fmt.Println("\n[scenario: insufficient samples]")
	r := createRelease(ctx, c, "insufficient", obsMS, minSamples)
	id, _ := r["id"].(string)
	time.Sleep(time.Duration(obsMS+250) * time.Millisecond)

	v := verdictOf(evaluate(ctx, c, id))
	m := metricsOf(v)
	rep.ok(v["health"] == "unknown" && fmt.Sprint(v["reasons"]) == "[samples_insufficient]" &&
		asInt(m["samples"]) < int64(minSamples),
		"below the minimum sample count yields unknown/samples_insufficient",
		evidenceLine(v))

	st, out := command(ctx, c, id, "advance", gen(r))
	rep.ok(st == 409 && out["applied"] == false,
		"advance with insufficient samples is refused",
		fmt.Sprintf("status=%d", st))
}

// 5) Blackout: the metrics pipeline returns no data at all — unknown, with
// metrics_missing, never an implicit healthy.
func scenarioBlackout(ctx context.Context, c *signclient.Client, rep *report, obsMS, minSamples int) {
	fmt.Println("\n[scenario: blackout — metrics pipeline unavailable]")
	r := createRelease(ctx, c, "blackout", obsMS, minSamples)
	id, _ := r["id"].(string)
	time.Sleep(time.Duration(obsMS+250) * time.Millisecond)

	v := verdictOf(evaluate(ctx, c, id))
	rep.ok(v["health"] == "unknown" &&
		strings.Contains(fmt.Sprint(v["reasons"]), "metrics_missing"),
		"missing metrics yield unknown/metrics_missing (not healthy)",
		evidenceLine(v))
}

// 6) Rollback then late success: a rolled-back release cannot be resurrected
// even if later metrics are healthy; the stale/late verdict is recorded.
func scenarioRollbackLate(ctx context.Context, c *signclient.Client, rep *report, obsMS, minSamples int) {
	fmt.Println("\n[scenario: rollback followed by late healthy metrics]")
	r := createRelease(ctx, c, "degraded", obsMS, minSamples)
	id, _ := r["id"].(string)
	time.Sleep(time.Duration(obsMS+250) * time.Millisecond)

	_, out := command(ctx, c, id, "rollback", gen(r))
	rolledGen := int64(asInt(out["release"].(map[string]any)["generation"]))

	// Any further command must be refused and flagged as late.
	st, out2 := command(ctx, c, id, "advance", rolledGen)
	rej, _ := out2["rejection"].(map[string]any)
	late := false
	if rej != nil {
		if rv, ok := rej["verdict"].(map[string]any); ok {
			late = rv["late"] == true
		}
	}
	rep.ok(st == 409 && late,
		"after rollback, even a late healthy verdict cannot resurrect the release",
		fmt.Sprintf("status=%d late=%v reason=%v", st, late, rejSafe(rej)))

	cur := release(ctx, c, id)
	rep.ok(cur["state"] == "rolled_back",
		"release remains rolled_back",
		fmt.Sprintf("state=%v stage=%v", cur["state"], cur["stage"]))
}

// 7) Threshold freezing + expected-generation concurrency + retry safety.
func scenarioFreezeAndGeneration(ctx context.Context, c *signclient.Client, rep *report, obsMS, minSamples int) {
	fmt.Println("\n[scenario: threshold freeze & expected generation]")
	r := createRelease(ctx, c, "healthy", obsMS, minSamples)
	id, _ := r["id"].(string)

	// Freeze check: the release records v1 regardless of later global changes.
	rep.ok(asInt(r["threshold_version"]) == 1,
		"the release froze the threshold version at start",
		fmt.Sprintf("threshold_version=%v snapshot=%v", r["threshold_version"], r["threshold_snapshot"]))

	// Tighten the global policy; the release must keep its frozen snapshot.
	newBody, _ := json.Marshal(map[string]any{
		"error_rate_upper": 0.001, "latency_mean_ms": 10, "description": "acceptance-tighten",
	})
	resp, _, err := c.Do(ctx, "POST", "/api/thresholds", newBody, "")
	rep.ok(err == nil && resp.StatusCode == 201,
		"a new global threshold version can be published",
		fmt.Sprintf("status=%d", resp.StatusCode))

	again := release(ctx, c, id)
	rep.ok(asInt(again["threshold_version"]) == 1 &&
		again["threshold_snapshot"].(map[string]any)["error_rate_upper"] == 0.05,
		"the running release keeps its frozen v1 snapshot after a global policy change",
		fmt.Sprintf("version=%v snapshot=%v", again["threshold_version"], again["threshold_snapshot"]))

	time.Sleep(time.Duration(obsMS+250) * time.Millisecond)
	st, out := command(ctx, c, id, "advance", gen(r))
	rep.ok(st == 200 && out["applied"] == true, "first advance applies (gen 0 -> 1)",
		fmt.Sprintf("status=%d gen=%v", st, out["release"].(map[string]any)["generation"]))

	// Replay the same command with the stale generation: must conflict, never
	// execute twice.
	st2, _ := command(ctx, c, id, "advance", gen(r))
	rep.ok(st2 == 409,
		"retrying advance with the stale generation 0 conflicts (no double execution)",
		fmt.Sprintf("status=%d", st2))

	cur := release(ctx, c, id)
	rep.ok(asInt(cur["generation"]) == 1 && cur["stage"] == "20%",
		"state shows exactly one applied advance",
		fmt.Sprintf("generation=%v stage=%v", cur["generation"], cur["stage"]))
}

// ---- small helpers ----

func gen(r map[string]any) int64 { return int64(asInt(r["generation"])) }

func asInt(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case int64:
		return n
	case int:
		return int64(n)
	case json.Number:
		i, _ := n.Int64()
		return i
	}
	return 0
}

func rejSafe(r map[string]any) any {
	if r == nil {
		return nil
	}
	return r["reason"]
}

func decode(b []byte) map[string]any {
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		fatalf("decode %q: %v", string(b), err)
	}
	return m
}

func fatalf(format string, args ...any) {
	fmt.Printf("FATAL: "+format+"\n", args...)
	os.Exit(1)
}
