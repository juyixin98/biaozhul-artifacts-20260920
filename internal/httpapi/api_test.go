package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"deadlockcheck/internal/httpapi"
	"deadlockcheck/internal/service"
	"deadlockcheck/internal/store"
)

func dsn() string {
	if v := os.Getenv("DATABASE_DSN"); v != "" {
		return v
	}
	// Separate database from the service package tests, which run in
	// parallel as separate packages and otherwise share table state.
	return "postgres://deadlock:deadlock_pw_068@localhost:5432/deadlock_db_http?sslmode=disable"
}

func newServer(t *testing.T) http.Handler {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, dsn())
	if err != nil {
		t.Skipf("postgresql unavailable: %v", err)
	}
	t.Cleanup(st.Close)
	for _, q := range []string{
		`TRUNCATE task_events, hold_ledger, task_resources, tasks, resources RESTART IDENTITY CASCADE`,
		`DELETE FROM meta WHERE key IN ('fence_epoch','evidence_seed','server_id')`,
	} {
		if _, err := st.Pool.Exec(ctx, q); err != nil {
			t.Fatal(err)
		}
	}
	svc, err := service.New(ctx, st, service.Config{
		AgingStep: time.Second, AgingBonusPerStep: 1, AgingCap: 1000,
		DefaultDeadline: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	return httpapi.New(svc)
}

func do(t *testing.T, h http.Handler, method, path string, body any, into any) int {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if into != nil && rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), into); err != nil {
			t.Fatalf("decode %s: %v body=%s", path, err, rec.Body.String())
		}
	}
	return rec.Code
}

// TestHTTPEndToEnd drives the full cycle over the real HTTP stack:
// register -> grant -> wait -> uncertain fencing -> confirm stop -> promote.
func TestHTTPEndToEnd(t *testing.T) {
	h := newServer(t)

	for _, r := range []map[string]string{
		{"id": "tool-r", "kind": "tool"},
	} {
		if code := do(t, h, "POST", "/v1/resources", r, nil); code != 201 {
			t.Fatalf("register resource code=%d", code)
		}
	}

	var granted map[string]any
	if code := do(t, h, "POST", "/v1/tasks", map[string]any{
		"id": "A", "priority": 100, "resources": []string{"tool-r"}, "deadlineMs": 200,
	}, &granted); code != 201 || granted["status"] != "granted" {
		t.Fatalf("A code=%d body=%v", code, granted)
	}
	epoch := int64(granted["task"].(map[string]any)["fenceEpoch"].(float64))
	if epoch == 0 {
		t.Fatal("epoch should be initialized")
	}

	var waiting map[string]any
	if code := do(t, h, "POST", "/v1/tasks", map[string]any{
		"id": "B", "priority": 100, "resources": []string{"tool-r"}, "deadlineMs": 5000,
	}, &waiting); code != 201 || waiting["status"] != "waiting" {
		t.Fatalf("B code=%d body=%v", code, waiting)
	}

	// Wait-reason evidence before timeout.
	var wr map[string]any
	if code := do(t, h, "GET", "/v1/tasks/B/waits", nil, &wr); code != 200 {
		t.Fatalf("waits code=%d", code)
	}
	reasons := wr["waitReasons"].([]any)
	if len(reasons) != 1 {
		t.Fatalf("want 1 wait reason, got %d", len(reasons))
	}

	// Wait past a generous multiple of the 200ms lease (race builds are slow).
	time.Sleep(700 * time.Millisecond)
	var sweep map[string]any
	do(t, h, "POST", "/v1/sweep-timeouts", nil, &sweep)
	if int(sweep["count"].(float64)) != 1 {
		t.Fatalf("sweep count=%v", sweep)
	}

	// B stays blocked on the uncertain holder; reason text explains fencing.
	do(t, h, "GET", "/v1/tasks/B/waits", nil, &wr)
	r0 := wr["waitReasons"].([]any)[0].(map[string]any)
	if r0["holderState"] != "uncertain" {
		t.Fatalf("holder state = %v", r0["holderState"])
	}

	// Evidence token for A's grant verifies via the API.
	var detail map[string]any
	do(t, h, "GET", "/v1/tasks/A", nil, &detail)
	holds := detail["holds"].([]any)
	token := holds[0].(map[string]any)["evidenceToken"].(string)
	var verified map[string]any
	if code := do(t, h, "POST", "/v1/verify-token",
		map[string]any{"token": token}, &verified); code != 200 {
		t.Fatalf("verify code=%d", code)
	}
	if verified["taskId"] != "A" {
		t.Fatalf("verified payload %v", verified)
	}

	// Confirm stop -> B promoted.
	do(t, h, "POST", "/v1/tasks/A/confirm-stop", nil, nil)
	do(t, h, "GET", "/v1/tasks/B", nil, &detail)
	if detail["task"].(map[string]any)["state"] != "running" {
		t.Fatalf("B state after stop = %v", detail["task"])
	}
}

// TestHTTPCycleRejectedOverAPI confirms the API surfaces a 409 with cycle
// details for a deadlock-forming dynamic request.
func TestHTTPCycleRejectedOverAPI(t *testing.T) {
	h := newServer(t)
	do(t, h, "POST", "/v1/resources", map[string]string{"id": "x", "kind": "tool"}, nil)
	do(t, h, "POST", "/v1/resources", map[string]string{"id": "y", "kind": "tool"}, nil)
	do(t, h, "POST", "/v1/tasks", map[string]any{"id": "A", "resources": []string{"x"}, "deadlineMs": 60000}, nil)
	do(t, h, "POST", "/v1/tasks", map[string]any{"id": "B", "resources": []string{"y"}, "deadlineMs": 60000}, nil)
	do(t, h, "POST", "/v1/tasks/A/resources", map[string]any{"resources": []string{"y"}}, nil)

	var rejected map[string]any
	code := do(t, h, "POST", "/v1/tasks/B/resources",
		map[string]any{"resources": []string{"x"}}, &rejected)
	if code != 409 || rejected["status"] != "rejected" {
		t.Fatalf("want 409 rejected, code=%d body=%v", code, rejected)
	}
	cycles := rejected["cycles"].([]any)
	if len(cycles) == 0 {
		t.Fatal("cycles missing from rejection")
	}
}
