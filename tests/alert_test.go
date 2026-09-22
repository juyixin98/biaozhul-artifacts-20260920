package tests

import (
	"fmt"
	"net/http"
	"testing"
	"time"
)

// fireRateAlert creates a rate rule and returns the id of its first alert.
func fireRateAlert(t *testing.T, env *testEnv, tk tokens, slug string) (int64, int64) {
	orgID := env.orgID(t, slug)
	ruleID := rateRule(t, env, tk.Admin, slug, 300, 1)
	base := time.Date(2026, 9, 20, 10, 0, 0, 0, time.UTC)
	evs := mkEvents("al", "u", base, 2, 10)
	path, body := batchPayload(slug, "s", evs)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, ruleID, 1); n != 1 {
		t.Fatalf("want 1 alert, got %d", n)
	}
	return ruleID, firstRateAlertID(t, env, orgID, ruleID, 1)
}

// TestAlertLifecycleAndOptimisticVersion covers open -> investigating ->
// resolved, stale expected_version rejection, terminal-state guard, and the
// retained revision trail.
func TestAlertLifecycleAndOptimisticVersion(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("lifecycle")
	tk := env.seedOrg(t, slug, "UTC")
	_, alertID := fireRateAlert(t, env, tk, slug)

	// Wrong expected version -> 409, no state change.
	st, out := env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:transition", slug, alertID),
		tk.Analyst,
		map[string]any{"to_status": "investigating", "expected_version": 99})
	env.mustStatus(t, st, http.StatusConflict, out)
	if out["code"] != "version_conflict" {
		t.Fatalf("code=%v want version_conflict", out["code"])
	}
	if alertStatus(t, env, alertID) != "open" {
		t.Fatal("stale transition must not change status")
	}

	// First analyst moves to investigating.
	st, out = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:transition", slug, alertID),
		tk.Analyst,
		map[string]any{"to_status": "investigating", "expected_version": 1,
			"note": "taking it"})
	env.mustStatus(t, st, 200, out)
	if v := alertVersion(t, env, alertID); v != 2 {
		t.Fatalf("version=%d want 2", v)
	}

	// Concurrent decision with the same old version: one wins, one gets 409.
	path := fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:transition", slug, alertID)
	type r2 struct {
		status int
		ver    int32
	}
	resCh := make(chan r2, 2)
	for _, token := range []string{tk.Analyst, tk.Admin} {
		go func(token string) {
			code, body := env.do(t, http.MethodPost, path, token,
				map[string]any{"to_status": "resolved", "expected_version": 2})
			resCh <- r2{status: code, ver: int32(intOr(asMap(body["details"])["current_version"]))}
		}(token)
	}
	var ok, conflict int
	for i := 0; i < 2; i++ {
		r := <-resCh
		switch r.status {
		case 200:
			ok++
		case http.StatusConflict:
			conflict++
		default:
			t.Fatalf("unexpected status %d", r.status)
		}
	}
	if ok != 1 || conflict != 1 {
		t.Fatalf("concurrent transitions: ok=%d conflict=%d, want 1/1", ok, conflict)
	}
	if alertStatus(t, env, alertID) != "resolved" {
		t.Fatalf("status=%s want resolved", alertStatus(t, env, alertID))
	}

	// Terminal state cannot transition.
	st, out = env.do(t, http.MethodPost, path, tk.Analyst,
		map[string]any{"to_status": "investigating", "expected_version": 3})
	env.mustStatus(t, st, http.StatusConflict, out)
	if out["code"] != "illegal_transition" {
		t.Fatalf("code=%v want illegal_transition", out["code"])
	}

	// Revision trail retained.
	revs := alertRevisions(t, env, slug, tk.Auditor, alertID)
	var transitions int
	for _, r := range revs {
		if asMap(r)["revision"] == "transition" {
			transitions++
		}
	}
	if transitions != 2 {
		t.Fatalf("transition history=%d want 2 (investigating, resolved)", transitions)
	}
}

// TestFalsePositiveFlow covers open -> false_positive directly.
func TestFalsePositiveFlow(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("fp")
	tk := env.seedOrg(t, slug, "UTC")
	_, alertID := fireRateAlert(t, env, tk, slug)

	st, out := env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:transition", slug, alertID),
		tk.Analyst,
		map[string]any{"to_status": "false_positive", "expected_version": 1,
			"note": "benign burst from cron"})
	env.mustStatus(t, st, 200, out)
	if alertStatus(t, env, alertID) != "false_positive" {
		t.Fatal("false_positive transition failed")
	}
}

// TestRecomputeDoesNotOverrideVerdict: resolve an alert, then late events
// arrive. The recomputation path must append evidence but leave the
// resolution and the analyst note/history intact.
func TestRecomputeDoesNotOverrideVerdict(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("keepverdict")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)
	ruleID := rateRule(t, env, tk.Admin, slug, 300, 1)
	base := time.Date(2026, 9, 20, 8, 0, 0, 0, time.UTC)
	evs := mkEvents("k1", "u", base, 2, 10)
	path, body := batchPayload(slug, "s", evs)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	alertID := firstRateAlertID(t, env, orgID, ruleID, 1)

	st, out = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:transition", slug, alertID),
		tk.Analyst,
		map[string]any{"to_status": "resolved", "expected_version": 1,
			"note": "investigated: real but handled"})
	env.mustStatus(t, st, 200, out)
	verdictVersion := alertVersion(t, env, alertID)

	// Two late events land in the same window.
	late := []batchEvent{
		{EventID: "k-late1", DbUser: "u",
			OccurredAt: base.Add(3 * time.Second).Format(time.RFC3339),
			Action:     "select", Table: "t", RowCount: 1},
		{EventID: "k-late2", DbUser: "u",
			OccurredAt: base.Add(7 * time.Second).Format(time.RFC3339),
			Action:     "select", Table: "t", RowCount: 1},
	}
	path, body = batchPayload(slug, "s", late)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)

	if alertStatus(t, env, alertID) != "resolved" {
		t.Fatalf("recompute overrode verdict: %s", alertStatus(t, env, alertID))
	}
	if v := alertVersion(t, env, alertID); v != verdictVersion {
		t.Fatalf("recompute bumped optimistic version %d -> %d", verdictVersion, v)
	}
	if got := rateAlertCount(t, env, alertID); got != 4 {
		t.Fatalf("evidence=%d want 4 (recompute appends only)", got)
	}

	revs := alertRevisions(t, env, slug, tk.Auditor, alertID)
	var verdictNote string
	var recomputes int
	for _, r := range revs {
		m := asMap(r)
		if m["revision"] == "transition" && m["to_status"] == "resolved" {
			verdictNote, _ = m["note"].(string)
		}
		if m["revision"] == "recompute" {
			recomputes++
		}
	}
	if verdictNote != "investigated: real but handled" {
		t.Fatalf("verdict note was altered: %q", verdictNote)
	}
	if recomputes != 1 {
		t.Fatalf("recompute revisions=%d want 1", recomputes)
	}
}
