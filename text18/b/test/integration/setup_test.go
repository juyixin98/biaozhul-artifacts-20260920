package integration

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"sircc/internal/config"
	"sircc/internal/httpapi"
	"sircc/internal/service"
)

// truncateAll resets the database between tests; CASCADE handles FK order.
func truncateAll(t *testing.T) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		TRUNCATE notifications, reminder_dispatches, action_item_reschedules,
		         action_items, evidence_notes, evidence, stage_events,
		         incident_members, incidents, audit_events, idempotent_requests,
		         users RESTART IDENTITY CASCADE`)
	if err != nil {
		t.Fatalf("truncate: %v", err)
	}
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func newService(t *testing.T) *service.Service {
	t.Helper()
	cfg := config.Config{
		ReminderInterval:  50 * time.Millisecond,
		ReminderBatchSize: 100,
	}
	return service.New(pool, cfg)
}

func newServer(t *testing.T, svc *service.Service) http.Handler {
	t.Helper()
	return httpapi.Router(svc, svc.Q())
}

// ---- small HTTP client helpers ----

func doJSON(t *testing.T, h http.Handler, method, target, user, requestID, body string) *httptest.ResponseRecorder {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, target, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, target, nil)
	}
	if user != "" {
		r.Header.Set("X-User-Id", user)
	}
	if requestID != "" {
		r.Header.Set("X-Request-Id", requestID)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func reqID() string { return "req-" + randomHex(8) }
