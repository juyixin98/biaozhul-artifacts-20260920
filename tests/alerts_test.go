package tests

import (
	"context"
	"net/http"
	"testing"
	"time"

	"dams/internal/db"
)

func oneSensitiveAlert(t *testing.T, h *Harness) map[string]any {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	at := time.Date(2026, 3, 5, 21, 0, 0, 0, loc)
	e := baseEvent("x1", at)
	e["schema_name"] = "public"
	e["table_name"] = "salaries"
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": []map[string]any{e}})
	alerts := h.Alerts("")
	if len(alerts) != 1 {
		t.Fatalf("want 1 alert, got %d", len(alerts))
	}
	return alerts[0].(map[string]any)
}

// Full lifecycle: pending -> investigating -> resolved, with expected-version
// optimistic locking and append-only status history.
func TestAlertLifecycleAndOptimisticVersion(t *testing.T) {
	h := Setup(t, "Asia/Shanghai")
	h.CreateRule("sensitive_hours", map[string]any{
		"sensitive_tables":   []map[string]string{{"schema": "public", "table": "salaries"}},
		"allowed_start_hour": 6,
		"allowed_end_hour":   20,
		"actions":            []string{"select"},
	}, time.Unix(0, 0))

	a := oneSensitiveAlert(t, h)
	id := int64(a["id"].(float64))
	path := "/v1/alerts/" + itoa(id) + ":transition"

	// Stale version (alert is at version 1, client thinks 9) → 409, unchanged.
	rec, _ := h.Do(http.MethodPost, path, h.Keys.Analyst, map[string]any{
		"expected_version": 9, "status": "investigating", "note": "x",
	})
	if rec.StatusCode != http.StatusConflict {
		t.Fatalf("stale version: status = %d, want 409", rec.StatusCode)
	}

	// Correct version → investigating, version becomes 2.
	body := h.MustStatus(http.MethodPost, path, h.Keys.Analyst, http.StatusOK, map[string]any{
		"expected_version": 1, "status": "investigating", "note": "looking into it",
	})
	if body["version"].(float64) != 2 || body["status"] != "investigating" {
		t.Fatalf("unexpected alert after transition: %v", body)
	}

	// Resolve with the current version.
	h.MustStatus(http.MethodPost, path, h.Keys.Analyst, http.StatusOK, map[string]any{
		"expected_version": 2, "status": "resolved", "note": "confirmed cleanup job",
	})

	// Re-resolving an already resolved alert is not a legal transition.
	rec, _ = h.Do(http.MethodPost, path, h.Keys.Analyst, map[string]any{
		"expected_version": 3, "status": "resolved",
	})
	if rec.StatusCode != http.StatusConflict {
		t.Fatalf("resolved -> resolved: status = %d, want 409", rec.StatusCode)
	}

	detail := h.MustStatus(http.MethodGet, "/v1/alerts/"+itoa(id), h.Keys.Analyst, http.StatusOK, nil)
	history := detail["history"].([]any)
	if len(history) != 2 {
		t.Fatalf("status history = %d entries, want 2", len(history))
	}
	h0 := history[0].(map[string]any)
	if h0["from_status"] != "pending" || h0["to_status"] != "investigating" {
		t.Fatalf("first history row wrong: %v", h0)
	}
	evidence := detail["evidence"].([]any)
	if len(evidence) != 1 {
		t.Fatalf("evidence revisions = %d, want 1", len(evidence))
	}
}

// Recomputation may append evidence but must not overwrite an investigator's
// decision.
func TestRecomputeDoesNotOverrideVerdict(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 6, 10, 0, 0, 0, time.UTC)
	h.CreateRule("frequency", map[string]any{
		"window_seconds": 300, "threshold": 500, "actions": freqActions,
	}, time.Unix(0, 0))
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": burstEvents("dave", t0, 501, 0)})

	alerts := h.Alerts("")
	if len(alerts) != 5 {
		t.Fatalf("want 5 alerts, got %d", len(alerts))
	}
	// Analyst declares the first alert a false positive.
	var target map[string]any
	for _, a := range alerts {
		target = a.(map[string]any)
		break
	}
	id := int64(target["id"].(float64))
	h.MustStatus(http.MethodPost, "/v1/alerts/"+itoa(id)+":transition", h.Keys.Analyst, http.StatusOK,
		map[string]any{"expected_version": 1, "status": "false_positive", "note": "batch job"})

	// Late backfill triggers recomputation of that window (evidence appended).
	late := make([]map[string]any, 5)
	for i := range late {
		e := baseEvent("L"+itoa(int64(i)), t0.Add(-200*time.Second+time.Duration(i)*time.Second))
		e["db_user"] = "dave"
		late[i] = e
	}
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": late})

	row, err := db.New(h.Pool).GetAlert(context.Background(),
		db.GetAlertParams{OrgID: h.Org.ID, ID: id})
	if err != nil {
		t.Fatal(err)
	}
	if row.Status != "false_positive" {
		t.Fatalf("verdict overwritten by recompute: status=%s", row.Status)
	}
	if row.Version != 2 {
		t.Fatalf("version changed by recompute: %d", row.Version)
	}
	revs, _ := db.New(h.Pool).ListEvidenceRevisions(context.Background(), id)
	if len(revs) < 1 {
		t.Fatalf("expected evidence preserved")
	}
}

// Manual recompute endpoint returns the recount and is audited.
func TestManualRecompute(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 7, 10, 0, 0, 0, time.UTC)
	h.CreateRule("frequency", map[string]any{
		"window_seconds": 300, "threshold": 500, "actions": freqActions,
	}, time.Unix(0, 0))
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": burstEvents("erin", t0, 501, 0)})

	out := h.MustStatus(http.MethodPost, "/v1/alerts/recompute", h.Keys.Analyst, http.StatusOK, map[string]any{
		"db_user":      "erin",
		"window_start": rfc(t0),
	})
	if out["event_count"].(float64) != 501 {
		t.Fatalf("recompute count = %v, want 501", out["event_count"])
	}
	if out["violating"] != true {
		t.Fatalf("recompute violating = %v, want true", out["violating"])
	}
	if out["alert_created"] != false {
		t.Fatalf("recompute must not duplicate the existing alert")
	}
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
