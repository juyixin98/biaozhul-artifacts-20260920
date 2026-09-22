package tests

import (
	"context"
	"fmt"
	"net/http"
	"testing"
	"time"
)

func createRule(t *testing.T, env *testEnv, token, org string, body map[string]any) (int, map[string]any) {
	return env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/rules", org), token, body)
}

// rateRule creates a rate rule and returns its id.
func rateRule(t *testing.T, env *testEnv, token, org string, windowSec, maxEv int32) int64 {
	st, out := createRule(t, env, token, org, map[string]any{
		"kind": "rate", "name": "rate-test",
		"window_seconds": windowSec, "max_events": maxEv,
	})
	env.mustStatus(t, st, http.StatusCreated, out)
	return int64(out["id"].(float64))
}

// TestRateWindowBoundary exercises the half-open fixed window
// [start, start+window): with threshold 3, 3 events inside do not fire;
// a 4th inside fires once; an event exactly on the closing boundary belongs
// to the next window and does not add to the first alert's evidence.
func TestRateWindowBoundary(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("ratebound")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)
	ruleID := rateRule(t, env, tk.Admin, slug, 300, 3)

	base := time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC) // aligned to 300s

	// 3 events at 12:00:00, 12:01:00, 12:04:59 -> no fire (<= 3).
	evs := []batchEvent{
		{EventID: "b1", DbUser: "u", OccurredAt: base.Format(time.RFC3339),
			Action: "select", Table: "t", RowCount: 1},
		{EventID: "b2", DbUser: "u",
			OccurredAt: base.Add(60 * time.Second).Format(time.RFC3339),
			Action:     "select", Table: "t", RowCount: 1},
		{EventID: "b3", DbUser: "u",
			OccurredAt: base.Add(299 * time.Second).Format(time.RFC3339),
			Action:     "select", Table: "t", RowCount: 1},
	}
	path, body := batchPayload(slug, "s", evs)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, ruleID, 1); n != 0 {
		t.Fatalf("expected 0 alert with exactly 3 events, got %d", n)
	}

	// 4th event inside the window (12:02:00, out of order) -> one alert.
	late := []batchEvent{{
		EventID: "b4", DbUser: "u",
		OccurredAt: base.Add(120 * time.Second).Format(time.RFC3339),
		Action:     "select", Table: "t", RowCount: 1,
	}}
	path, body = batchPayload(slug, "s", late)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, ruleID, 1); n != 1 {
		t.Fatalf("expected 1 alert with 4 events, got %d", n)
	}
	alertID := firstRateAlertID(t, env, orgID, ruleID, 1)
	if got := rateAlertCount(t, env, alertID); got != 4 {
		t.Fatalf("alert evidence count=%d, want 4", got)
	}

	// Event exactly on the closing boundary 12:05:00 belongs to the NEXT
	// window. Re-send the same triggering batch: alert must not change.
	boundary := []batchEvent{{
		EventID: "b5", DbUser: "u",
		OccurredAt: base.Add(300 * time.Second).Format(time.RFC3339),
		Action:     "select", Table: "t", RowCount: 1,
	}}
	path, body = batchPayload(slug, "s", boundary)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, ruleID, 1); n != 1 {
		t.Fatalf("boundary event must open a new window, not a second alert; got %d v1 alerts", n)
	}
	if got := rateAlertCount(t, env, alertID); got != 4 {
		t.Fatalf("first alert evidence changed to %d, must stay 4", got)
	}
}

// TestLateOutOfOrderRecompute: a window already alerted accumulates a late
// event; recomputation appends evidence and a 'recompute' revision without
// changing the alert status or producing a second alert.
func TestLateOutOfOrderRecompute(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("late")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)
	ruleID := rateRule(t, env, tk.Admin, slug, 300, 2)
	base := time.Date(2026, 9, 21, 9, 0, 0, 0, time.UTC)

	evs := mkEvents("late", "u", base, 3, 10) // 00,10,20 -> count 3 > 2
	path, body := batchPayload(slug, "s", evs)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, ruleID, 1); n != 1 {
		t.Fatalf("expected 1 alert, got %d", n)
	}
	alertID := firstRateAlertID(t, env, orgID, ruleID, 1)

	// Mark it investigating, then late event arrives. Investigator decision
	// must survive.
	st, out = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:transition", slug, alertID),
		tk.Admin, map[string]any{"to_status": "investigating", "expected_version": 1})
	env.mustStatus(t, st, 200, out)

	late := []batchEvent{{
		EventID: "late-x", DbUser: "u",
		OccurredAt: base.Add(5 * time.Second).Format(time.RFC3339),
		Action:     "select", Table: "t", RowCount: 1,
	}}
	path, body = batchPayload(slug, "s", late)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)

	if n := countRateAlerts(t, env, orgID, ruleID, 1); n != 1 {
		t.Fatalf("late event must not create a second alert, got %d", n)
	}
	status := alertStatus(t, env, alertID)
	if status != "investigating" {
		t.Fatalf("investigator decision overwritten: status=%s, want investigating", status)
	}
	if got := rateAlertCount(t, env, alertID); got != 4 {
		t.Fatalf("evidence not recomputed: count=%d want 4", got)
	}

	revs := alertRevisions(t, env, slug, tk.Analyst, alertID)
	var sawRecompute bool
	for _, r := range revs {
		if asMap(r)["revision"] == "recompute" {
			sawRecompute = true
		}
	}
	if !sawRecompute {
		t.Fatalf("expected an append-only 'recompute' revision, got %v", revs)
	}
}

// TestRuleVersionRebinding: alerts stay bound to the rule version that fired.
// Tightening a rule to v2 fires a new, version-2 alert; the v1 alert remains
// and its manual recompute still uses the v1 window parameters.
func TestRuleVersionRebinding(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("version")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)
	id := rateRule(t, env, tk.Admin, slug, 300, 5)
	base := time.Date(2026, 9, 21, 8, 0, 0, 0, time.UTC)

	evs := mkEvents("v1", "u", base, 6, 10) // 6 > 5 -> v1 alert
	path, body := batchPayload(slug, "s", evs)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, id, 1); n != 1 {
		t.Fatalf("want 1 v1 alert, got %d", n)
	}
	v1Alert := firstRateAlertID(t, env, orgID, id, 1)

	// New, tighter version: max 2.
	st, out = createRule(t, env, tk.Admin, slug, map[string]any{
		"rule_id": id, "kind": "rate", "name": "rate-test",
		"window_seconds": 300, "max_events": 2,
	})
	env.mustStatus(t, st, http.StatusCreated, out)
	if v := int(out["version"].(float64)); v != 2 {
		t.Fatalf("new version=%d want 2", v)
	}

	// New events landing in the SAME window are evaluated against the
	// current (v2) rule: v2 fires while the v1 alert stays untouched.
	more := mkEvents("v2more", "u", base, 3, 10)
	path2, body2 := batchPayload(slug, "s", more)
	st, out = env.do(t, http.MethodPost, path2, tk.Admin, body2)
	env.mustStatus(t, st, 200, out)
	if n := countRateAlerts(t, env, orgID, id, 2); n != 1 {
		t.Fatalf("want 1 v2 alert, got %d", n)
	}
	if n := countRateAlerts(t, env, orgID, id, 1); n != 1 {
		t.Fatalf("v1 alert must remain, got %d", n)
	}
	// Manual recompute on the v1 alert uses v1 window parameters and links
	// every event in that window (the original 6 plus the 3 newer ones).
	st, out = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/%d:recompute", slug, v1Alert),
		tk.Admin, map[string]any{})
	env.mustStatus(t, st, 200, out)
	if got := rateAlertCount(t, env, v1Alert); got != 9 {
		t.Fatalf("v1 recompute count=%d want 9", got)
	}
}

// TestSensitiveTimezoneBoundary checks 06:00 inclusive / 20:00 exclusive in
// the organization's IANA timezone (Asia/Shanghai, UTC+8 with no DST).
func TestSensitiveTimezoneBoundary(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("sens")
	tk := env.seedOrg(t, slug, "Asia/Shanghai")
	orgID := env.orgID(t, slug)

	st, out := createRule(t, env, tk.Admin, slug, map[string]any{
		"kind": "sensitive", "name": "sens-test",
		"tables":     []string{"public.customer_pii"},
		"hour_start": 6, "hour_end": 20,
	})
	env.mustStatus(t, st, http.StatusCreated, out)
	ruleID := int64(out["id"].(float64))
	version := int32(out["version"].(float64))

	cases := []struct {
		name string
		ts   string
		want bool // expect alert?
	}{
		{"05:59:59 local is outside", "2026-09-21T05:59:59+08:00", true},
		{"06:00:00 local is allowed", "2026-09-21T06:00:00+08:00", false},
		{"19:59:59 local is allowed", "2026-09-21T19:59:59+08:00", false},
		{"20:00:00 local is outside", "2026-09-21T20:00:00+08:00", true},
		{"22:30 local is outside", "2026-09-21T22:30:00+08:00", true},
		{"12:00 local is allowed", "2026-09-21T12:00:00+08:00", false},
	}
	for i, c := range cases {
		ev := []batchEvent{{
			EventID: fmt.Sprintf("sens-%d", i), DbUser: "u",
			OccurredAt: c.ts, Action: "select",
			Schema: "public", Table: "customer_pii", RowCount: 1,
		}}
		path, body := batchPayload(slug, "s", ev)
		st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
		env.mustStatus(t, st, 200, out)
	}
	var alerts int
	err := env.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM alerts WHERE org_id=$1 AND rule_id=$2 AND rule_version=$3`,
		orgID, ruleID, version).Scan(&alerts)
	if err != nil {
		t.Fatalf("count alerts: %v", err)
	}
	var want int
	for _, c := range cases {
		if c.want {
			want++
		}
	}
	if alerts != want {
		t.Fatalf("sensitive alerts=%d, want %d (cases=%+v)", alerts, want, cases)
	}
}
