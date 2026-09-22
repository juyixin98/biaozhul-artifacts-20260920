package tests

import (
	"context"
	"fmt"
	"net/http"
	"testing"
	"time"

	"dams/internal/db"
)

var freqActions = []string{"select", "insert", "update", "delete", "ddl", "grant"}

// burst inserts n events for a user within a 30-second span starting at
// base+10s, so the cluster is fully contained in five 5-minute stepped
// windows when the frequency threshold is 500.
func burstEvents(user string, base time.Time, n int, offset time.Duration) []map[string]any {
	out := make([]map[string]any, n)
	for i := 0; i < n; i++ {
		e := baseEvent(fmt.Sprintf("%s-%04d", user, i),
			base.Add(10*time.Second+offset+time.Duration(i)*60*time.Millisecond))
		e["db_user"] = user
		out[i] = e
	}
	return out
}

func findAlertByDetail(list []any, key, val string) map[string]any {
	for _, a := range list {
		m := a.(map[string]any)
		d := m["detail"].(map[string]any)
		if fmt.Sprint(d[key]) == val {
			return m
		}
	}
	return nil
}

// 501 events in a 30s cluster cross the 500/5min threshold in exactly five
// stepped windows → five alerts. A replay creates none.
func TestFrequencyBurstRaisesFiveAlerts(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 2, 10, 0, 0, 0, time.UTC)
	h.CreateRule("frequency", map[string]any{
		"window_seconds": 300, "threshold": 500, "actions": freqActions,
	}, time.Unix(0, 0))

	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": burstEvents("bob", t0, 501, 0)})

	alerts := h.Alerts("")
	if len(alerts) != 5 {
		t.Fatalf("alerts = %d, want 5", len(alerts))
	}

	// Exact replay must not raise anything new.
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": burstEvents("bob", t0, 501, 0)})
	if got := h.Alerts(""); len(got) != 5 {
		t.Fatalf("alerts after replay = %d, want 5", len(got))
	}
}

// A half-open [start, end) window: an event exactly at window end is NOT
// counted in that window. Uses a small threshold to stay readable.
func TestFrequencyWindowIsHalfOpen(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 2, 10, 0, 0, 0, time.UTC)
	h.CreateRule("frequency", map[string]any{
		"window_seconds": 300, "threshold": 5, "actions": []string{"select"},
	}, time.Unix(0, 0))

	evs := []map[string]any{}
	for i := 0; i < 5; i++ { // 5 events at t0..t0+4m, count == threshold => no alert
		evs = append(evs, baseEvent(fmt.Sprintf("b%02d", i), t0.Add(time.Duration(i)*time.Minute)))
	}
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": evs})
	// Event exactly at the window end belongs to the NEXT window, so the
	// authoritative recount of [t0, t0+5m) must still be exactly 5.
	count, start, end, err := h.Deps.Engine.CountWindow(context.Background(),
		db.New(h.Pool), h.Org.ID, "alice", t0, time.UTC)
	if err != nil {
		t.Fatal(err)
	}
	if !start.Equal(t0) || !end.Equal(t0.Add(5*time.Minute)) {
		t.Fatalf("window bounds = %s..%s", start, end)
	}
	if count != 5 {
		t.Fatalf("count in [t0,t0+5m) = %d, want 5", count)
	}
	if len(h.Alerts("")) != 0 {
		t.Fatalf("threshold not exceeded, expected no alerts")
	}

	// One more event strictly inside crosses to 6 (> 5) → alert.
	inside := baseEvent("b05", t0.Add(4*time.Minute+30*time.Second))
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": []map[string]any{inside}})
	if len(h.Alerts("")) == 0 {
		t.Fatalf("expected an alert once count exceeded threshold")
	}
}

// Sensitive-table access is allowed at 06:00 sharp and 19:xx, but flagged at
// 05:59:59 and 20:00 sharp in the organization timezone (half-open [6,20)).
func TestSensitiveHoursBoundaries(t *testing.T) {
	h := Setup(t, "Asia/Shanghai")
	h.CreateRule("sensitive_hours", map[string]any{
		"sensitive_tables":   []map[string]string{{"schema": "hr", "table": "employees"}},
		"allowed_start_hour": 6,
		"allowed_end_hour":   20,
		"actions":            []string{"select"},
	}, time.Unix(0, 0))

	loc, _ := time.LoadLocation("Asia/Shanghai")
	at := func(hh, mm, ss int) time.Time {
		return time.Date(2026, 3, 3, hh, mm, ss, 0, loc)
	}
	cases := []struct {
		name string
		t    time.Time
		want bool // alert expected
	}{
		{"05:59:59 outside", at(5, 59, 59), true},
		{"06:00:00 inside (inclusive start)", at(6, 0, 0), false},
		{"19:59:59 inside", at(19, 59, 59), false},
		{"20:00:00 outside (exclusive end)", at(20, 0, 0), true},
	}
	var evs []map[string]any
	for i, c := range cases {
		e := baseEvent(fmt.Sprintf("h%02d", i), c.t)
		e["schema_name"] = "hr"
		e["table_name"] = "employees"
		evs = append(evs, e)
	}
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": evs})

	alerts := h.Alerts("")
	if len(alerts) != 2 {
		t.Fatalf("sensitive alerts = %d, want exactly 2 (05:59:59 and 20:00:00)", len(alerts))
	}
}

// Late, out-of-order backfill recomputes the affected windows: one original
// alert gains an evidence revision (501 -> 511), no duplicate alerts are
// produced, and four other affected windows stay below threshold.
func TestLateEventRecomputesWindowsAndAppendsEvidence(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 4, 10, 0, 0, 0, time.UTC)
	h.CreateRule("frequency", map[string]any{
		"window_seconds": 300, "threshold": 500, "actions": freqActions,
	}, time.Unix(0, 0))

	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": burstEvents("carol", t0, 501, 0)})
	if len(h.Alerts("")) != 5 {
		t.Fatalf("expected 5 initial alerts")
	}

	// Ten late events at t0-200s: they land in five stepped windows, of which
	// only [t0-4m, t0+1m) overlaps the original cluster.
	late := make([]map[string]any, 10)
	for i := range late {
		e := baseEvent(fmt.Sprintf("late-%02d", i), t0.Add(-200*time.Second+time.Duration(i)*time.Second))
		e["db_user"] = "carol"
		late[i] = e
	}
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": late})

	alerts := h.Alerts("")
	if len(alerts) != 5 {
		t.Fatalf("alerts after late backfill = %d, still want 5 (no duplicates)", len(alerts))
	}

	affectedStart := t0.Add(-4 * time.Minute).UTC().Format(time.RFC3339Nano)
	a := findAlertByDetail(alerts, "window_start", affectedStart)
	if a == nil {
		t.Fatalf("alert for affected window %s not found", affectedStart)
	}
	alertID := int64(a["id"].(float64))

	q := db.New(h.Pool)
	revs, err := q.ListEvidenceRevisions(context.Background(), alertID)
	if err != nil {
		t.Fatal(err)
	}
	if len(revs) != 2 {
		t.Fatalf("evidence revisions = %d, want 2 (original + recomputation)", len(revs))
	}
	if revs[0].EventCount != 501 || revs[1].EventCount != 511 {
		t.Fatalf("evidence counts = %d -> %d, want 501 -> 511",
			revs[0].EventCount, revs[1].EventCount)
	}

	linked, err := q.ListAlertEvents(context.Background(), alertID)
	if err != nil {
		t.Fatal(err)
	}
	if len(linked) != 511 {
		t.Fatalf("alert linked to %d events after recompute, want 511", len(linked))
	}

	// One of the other four late-affected windows now holds exactly 10 events
	// and no alert (below threshold).
	cnt, err := q.CountEventsInWindow(context.Background(), db.CountEventsInWindowParams{
		OrgID: h.Org.ID, DbUser: "carol",
		WindowStart: pgxTS(t0.Add(-5 * time.Minute)),
		WindowEnd:   pgxTS(t0),
		Actions:     freqActions,
	})
	if err != nil {
		t.Fatal(err)
	}
	if cnt.EventCount != 10 {
		t.Fatalf("other late window count = %d, want 10", cnt.EventCount)
	}
}
