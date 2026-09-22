package tests

import (
	"context"
	"net/http"
	"testing"
	"time"

	"dams/internal/db"
)

func TestAuthenticationRequired(t *testing.T) {
	h := Setup(t, "UTC")
	rec, _ := h.Do(http.MethodGet, "/v1/alerts/", "", nil)
	if rec.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no token: status = %d, want 401", rec.StatusCode)
	}
	req := h.doRaw(http.MethodGet, "/v1/alerts/", "dams_nonexistent_key", nil)
	if req.Code != http.StatusUnauthorized {
		t.Fatalf("bad token: status = %d, want 401", req.Code)
	}
}

// Role matrix: collectors ingest but cannot configure or triage; analysts
// triage but not configure; auditors are read-only; only admins configure.
func TestRoleMatrix(t *testing.T) {
	h := Setup(t, "UTC")

	// analyst cannot ingest
	rec, _ := h.IngestAs(h.Keys.Analyst, "src", []map[string]any{
		baseEvent("p", time.Now()),
	})
	if rec.StatusCode != http.StatusForbidden {
		t.Fatalf("analyst ingest: %d, want 403", rec.StatusCode)
	}

	// auditor cannot create rules
	rec, _ = h.Do(http.MethodPost, "/v1/rules/", h.Keys.Auditor, map[string]any{
		"rule_type": "frequency",
		"params":    map[string]any{"threshold": 1},
	})
	if rec.StatusCode != http.StatusForbidden {
		t.Fatalf("auditor create rule: %d, want 403", rec.StatusCode)
	}

	// analyst cannot create rules
	rec, _ = h.Do(http.MethodPost, "/v1/rules/", h.Keys.Analyst, map[string]any{
		"rule_type": "frequency",
		"params":    map[string]any{"threshold": 1},
	})
	if rec.StatusCode != http.StatusForbidden {
		t.Fatalf("analyst create rule: %d, want 403", rec.StatusCode)
	}

	// auditor cannot transition alerts (need a real alert; 403 must come
	// before any 404/409 evaluation)
	rec, _ = h.Do(http.MethodPost, "/v1/alerts/1:transition", h.Keys.Auditor, map[string]any{
		"expected_version": 1, "status": "resolved",
	})
	if rec.StatusCode != http.StatusForbidden {
		t.Fatalf("auditor transition: %d, want 403", rec.StatusCode)
	}

	// auditor CAN read alerts and the audit chain
	h.MustStatus(http.MethodGet, "/v1/alerts/", h.Keys.Auditor, http.StatusOK, nil)
	h.MustStatus(http.MethodGet, "/v1/audit/verify", h.Keys.Auditor, http.StatusOK, nil)

	// collector cannot read alerts
	rec, _ = h.Do(http.MethodGet, "/v1/alerts/", h.Keys.Collector, nil)
	if rec.StatusCode != http.StatusForbidden {
		t.Fatalf("collector list alerts: %d, want 403", rec.StatusCode)
	}

	// admin can configure
	h.MustStatus(http.MethodPost, "/v1/rules/", h.Keys.Admin, http.StatusCreated, map[string]any{
		"rule_type": "frequency",
		"params":    map[string]any{"window_seconds": 300, "threshold": 500},
	})
}

func (h *Harness) IngestAs(key, source string, evs []map[string]any) (*http.Response, map[string]any) {
	return h.Do(http.MethodPost, "/v1/events:batch", key,
		map[string]any{"source": source, "events": evs})
}

// Export masks sensitive fields for EVERY role, including admin.
func TestExportMasksSensitiveFields(t *testing.T) {
	h := Setup(t, "UTC")
	e := baseEvent("m1", time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC))
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": []map[string]any{e}})

	for _, key := range []string{h.Keys.Admin, h.Keys.Analyst, h.Keys.Auditor} {
		body := h.MustStatus(http.MethodGet, "/v1/events", key, http.StatusOK, nil)
		events := body["events"].([]any)
		if len(events) != 1 {
			t.Fatalf("export event count = %d", len(events))
		}
		ev := events[0].(map[string]any)
		if ev["sql_text"] != "***MASKED***" || ev["client_ip"] != "***MASKED***" {
			t.Fatalf("sensitive fields not masked for key %s: %v", key[:14], ev)
		}
		if ev["table_name"] != "orders" || ev["row_count"].(float64) != 1 {
			t.Fatalf("non-sensitive fields altered: %v", ev)
		}
	}

	// Raw SQL text stays in the database but never leaves through export.
	rows, err := db.New(h.Pool).ListEventsByOrg(context.Background(), db.ListEventsByOrgParams{
		OrgID: h.Org.ID, RowLimit: 1, RowOffset: 0,
	})
	if err != nil || len(rows) != 1 {
		t.Fatalf("load stored event: %v (%d rows)", err, len(rows))
	}
	if rows[0].SqlText != "SELECT 1" {
		t.Fatalf("raw sql not stored: %q", rows[0].SqlText)
	}
}

// An analyst in org A cannot see or act on org B's alerts, even with a valid
// key for their own organization.
func TestCrossOrgIsolation(t *testing.T) {
	a := Setup(t, "UTC")
	b := Setup(t, "UTC")
	b.CreateRule("sensitive_hours", map[string]any{
		"sensitive_tables":   []map[string]string{{"schema": "public", "table": "salaries"}},
		"allowed_start_hour": 6,
		"allowed_end_hour":   20,
		"actions":            []string{"select"},
	}, time.Unix(0, 0))

	loc, _ := time.LoadLocation("UTC")
	e := baseEvent("b1", time.Date(2026, 3, 9, 3, 0, 0, 0, loc))
	e["schema_name"] = "public"
	e["table_name"] = "salaries"
	b.MustStatus(http.MethodPost, "/v1/events:batch", b.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": []map[string]any{e}})

	bAlerts := b.Alerts("")
	if len(bAlerts) != 1 {
		t.Fatalf("setup: expected 1 alert in org B")
	}
	id := int64(bAlerts[0].(map[string]any)["id"].(float64))

	// Org A analyst lists alerts — must be empty (no cross-visibility).
	aList := a.MustStatus(http.MethodGet, "/v1/alerts/", a.Keys.Analyst, http.StatusOK, nil)
	if aList["total"].(float64) != 0 {
		t.Fatalf("org A could see org B alerts: %v", aList)
	}
	// Org A analyst fetches org B's alert id directly — not found.
	rec, _ := a.Do(http.MethodGet, "/v1/alerts/"+itoa(id), a.Keys.Analyst, nil)
	if rec.StatusCode != http.StatusNotFound {
		t.Fatalf("cross-org GET: %d, want 404", rec.StatusCode)
	}
	// Org A analyst tries to transition org B's alert — not found, never acted on.
	rec, _ = a.Do(http.MethodPost, "/v1/alerts/"+itoa(id)+":transition", a.Keys.Analyst, map[string]any{
		"expected_version": 1, "status": "resolved",
	})
	if rec.StatusCode != http.StatusNotFound {
		t.Fatalf("cross-org transition: %d, want 404", rec.StatusCode)
	}
}
