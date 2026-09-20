package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/httpapi"
	"sircc/internal/migrate"
	"sircc/internal/reminder"
)

// Seeded users (see internal/migrate/migrations/0001_init.sql).
const (
	adminID    = "00000000-0000-0000-0000-0000000000a1"
	analyst1ID = "00000000-0000-0000-0000-0000000000a2"
	analyst2ID = "00000000-0000-0000-0000-0000000000a3"
	responder1 = "00000000-0000-0000-0000-0000000000b1"
	responder2 = "00000000-0000-0000-0000-0000000000b2"
)

var (
	testPool *pgxpool.Pool
	testSrv  *httptest.Server
)

func TestMain(m *testing.M) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = "postgres://sircc:sircc@127.0.0.1:5432/sircc_test?sslmode=disable"
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		fmt.Fprintln(os.Stderr, "connect test db:", err)
		os.Exit(1)
	}
	if err := migrate.Apply(ctx, pool); err != nil {
		fmt.Fprintln(os.Stderr, "migrate test db:", err)
		os.Exit(1)
	}
	testPool = pool
	testSrv = httptest.NewServer(httpapi.NewServer(pool).Router())
	code := m.Run()
	testSrv.Close()
	pool.Close()
	os.Exit(code)
}

// reset clears all case data between tests; seeded users stay.
func reset(t *testing.T) {
	t.Helper()
	_, err := testPool.Exec(context.Background(), `
		TRUNCATE reminders, action_items, evidence_notes, evidence,
		         audit_events, phase_transitions, incidents`)
	if err != nil {
		t.Fatal(err)
	}
}

// ---- HTTP helpers ----

func do(t *testing.T, method, path, userID string, body any) (int, []byte) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, testSrv.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if userID != "" {
		req.Header.Set("X-User-Id", userID)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, data
}

func decode(t *testing.T, data []byte) map[string]any {
	t.Helper()
	var v map[string]any
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatalf("decode %s: %v", data, err)
	}
	return v
}

func errCode(t *testing.T, data []byte) string {
	t.Helper()
	return decode(t, data)["error"].(map[string]any)["code"].(string)
}

// createIncident posts a new incident and returns its id and version (1).
func createIncident(t *testing.T, severity string) string {
	t.Helper()
	status, body := do(t, "POST", "/incidents", analyst1ID, map[string]any{
		"title": "test incident", "severity": severity,
	})
	if status != http.StatusCreated {
		t.Fatalf("create incident: %d %s", status, body)
	}
	return decode(t, body)["id"].(string)
}

func assign(t *testing.T, incidentID, responderID string) {
	t.Helper()
	status, body := do(t, "POST", "/incidents/"+incidentID+"/assign", adminID,
		map[string]any{"responderId": responderID})
	if status != http.StatusOK {
		t.Fatalf("assign: %d %s", status, body)
	}
}

func transition(t *testing.T, incidentID, userID, to string, version int64, requestID string) (int, []byte) {
	t.Helper()
	return do(t, "POST", "/incidents/"+incidentID+"/transitions", userID, map[string]any{
		"toStatus": to, "expectedVersion": version, "requestId": requestID,
	})
}

// driveToPostmortem walks an incident from detected to postmortem.
// The incident must already have an assignee (responder1).
func driveToPostmortem(t *testing.T, incidentID string) {
	t.Helper()
	steps := []struct {
		user string
		to   string
	}{
		{analyst1ID, "triaged"},
		{responder1, "contained"},
		{responder1, "eradicated"},
		{responder1, "recovered"},
		{responder1, "postmortem"},
	}
	for i, s := range steps {
		status, body := transition(t, incidentID, s.user, s.to, int64(i+2), "req-"+s.to+"-"+incidentID)
		if status != http.StatusOK {
			t.Fatalf("transition to %s: %d %s", s.to, status, body)
		}
	}
}

func addActionItem(t *testing.T, incidentID, ownerID string, dueAt time.Time) string {
	t.Helper()
	status, body := do(t, "POST", "/incidents/"+incidentID+"/action-items", responder1, map[string]any{
		"title": "follow-up", "ownerId": ownerID, "dueAt": dueAt.UTC().Format(time.RFC3339),
	})
	if status != http.StatusCreated {
		t.Fatalf("create action item: %d %s", status, body)
	}
	return decode(t, body)["id"].(string)
}

// ---- lifecycle & transitions ----

func TestLifecycleHappyPath(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	assign(t, id, responder1)
	driveToPostmortem(t, id)

	status, body := do(t, "PUT", "/incidents/"+id+"/postmortem", responder1, map[string]any{
		"rootCause": "unpatched service", "lessonsLearned": "patch faster",
	})
	if status != http.StatusOK {
		t.Fatalf("postmortem: %d %s", status, body)
	}
	addActionItem(t, id, responder1, time.Now().Add(24*time.Hour))

	status, body = transition(t, id, responder1, "closed", 7, "req-close-"+id)
	if status != http.StatusOK {
		t.Fatalf("close: %d %s", status, body)
	}

	// Export reflects the full phase history and both durations.
	status, body = do(t, "GET", "/incidents/"+id+"/export", analyst1ID, nil)
	if status != http.StatusOK {
		t.Fatalf("export: %d %s", status, body)
	}
	exp := decode(t, body)
	phases := exp["phases"].([]any)
	if len(phases) != 7 {
		t.Fatalf("expected 7 phase records, got %d", len(phases))
	}
	metrics := exp["metrics"].(map[string]any)
	if metrics["containmentDurationSeconds"] == nil {
		t.Fatal("containment duration should be set after containment")
	}
	if metrics["resolutionDurationSeconds"] == nil {
		t.Fatal("resolution duration should be set after close")
	}
}

func TestP1TriageRequiresAssignee(t *testing.T) {
	reset(t)
	id := createIncident(t, "P1")
	status, body := transition(t, id, analyst1ID, "triaged", 1, "req-t1")
	if status != http.StatusUnprocessableEntity || errCode(t, body) != "triage_gate" {
		t.Fatalf("expected triage_gate 422, got %d %s", status, body)
	}
	// P2 triages fine without an assignee.
	id2 := createIncident(t, "P2")
	status, body = transition(t, id2, analyst1ID, "triaged", 1, "req-t2")
	if status != http.StatusOK {
		t.Fatalf("P2 triage: %d %s", status, body)
	}
	// After assignment the P1 gate clears (assignment bumped the version to 2).
	assign(t, id, responder1)
	status, body = transition(t, id, analyst1ID, "triaged", 2, "req-t3")
	if status != http.StatusOK {
		t.Fatalf("P1 triage after assign: %d %s", status, body)
	}
}

func TestTransitionIdempotency(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	status, body := transition(t, id, analyst1ID, "triaged", 1, "req-same")
	if status != http.StatusOK {
		t.Fatalf("first: %d %s", status, body)
	}
	first := decode(t, body)["transition"].(map[string]any)

	// Same requestId replays the original result without re-applying.
	status, body = transition(t, id, analyst1ID, "triaged", 1, "req-same")
	if status != http.StatusOK {
		t.Fatalf("replay: %d %s", status, body)
	}
	replay := decode(t, body)
	if replay["replayed"] != true {
		t.Fatal("expected replayed=true")
	}
	second := replay["transition"].(map[string]any)
	if second["id"] != first["id"] || second["versionAfter"] != first["versionAfter"] {
		t.Fatalf("replay returned a different transition: %v vs %v", second, first)
	}

	// Incident version is still 2: the replay did not move anything.
	_, body = do(t, "GET", "/incidents/"+id, analyst1ID, nil)
	if got := decode(t, body)["version"].(float64); got != 2 {
		t.Fatalf("version after replay = %v, want 2", got)
	}
}

func TestStaleVersionRejected(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	status, body := transition(t, id, analyst1ID, "triaged", 5, "req-v1")
	if status != http.StatusConflict || errCode(t, body) != "version_conflict" {
		t.Fatalf("expected version_conflict 409, got %d %s", status, body)
	}
}

func TestCannotSkipPhase(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	assign(t, id, responder1)
	status, body := transition(t, id, responder1, "contained", 2, "req-skip")
	if status != http.StatusConflict || errCode(t, body) != "invalid_transition" {
		t.Fatalf("expected invalid_transition 409, got %d %s", status, body)
	}
	// No transition or audit rows were written: no half-update.
	_, body = do(t, "GET", "/incidents/"+id+"/audit", analyst1ID, nil)
	if got := len(mustArray(t, body)); got != 2 { // created + assigned only
		t.Fatalf("audit events = %d, want 2", got)
	}
}

func TestConcurrentTransitions(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	const n = 10
	var wg sync.WaitGroup
	statuses := make([]int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			status, _ := transition(t, id, analyst1ID, "triaged", 1, fmt.Sprintf("req-race-%d", i))
			statuses[i] = status
		}(i)
	}
	wg.Wait()
	wins := 0
	for _, s := range statuses {
		if s == http.StatusOK {
			wins++
		} else if s != http.StatusConflict {
			t.Fatalf("unexpected status %d", s)
		}
	}
	if wins != 1 {
		t.Fatalf("exactly one concurrent transition may win, got %d", wins)
	}
	_, body := do(t, "GET", "/incidents/"+id, analyst1ID, nil)
	if got := decode(t, body)["version"].(float64); got != 2 {
		t.Fatalf("version = %v, want 2", got)
	}
}

func TestCloseGate(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	assign(t, id, responder1)
	driveToPostmortem(t, id)

	// Missing root cause / lessons / action items.
	status, body := transition(t, id, responder1, "closed", 7, "req-close-1")
	if status != http.StatusUnprocessableEntity || errCode(t, body) != "close_gate" {
		t.Fatalf("expected close_gate 422, got %d %s", status, body)
	}
	// Postmortem fields alone are not enough.
	do(t, "PUT", "/incidents/"+id+"/postmortem", responder1, map[string]any{
		"rootCause": "rc", "lessonsLearned": "ll",
	})
	status, body = transition(t, id, responder1, "closed", 7, "req-close-2")
	if status != http.StatusUnprocessableEntity || errCode(t, body) != "close_gate" {
		t.Fatalf("expected close_gate 422 without action items, got %d %s", status, body)
	}
	// All requirements met.
	addActionItem(t, id, responder1, time.Now().Add(24*time.Hour))
	status, body = transition(t, id, responder1, "closed", 7, "req-close-3")
	if status != http.StatusOK {
		t.Fatalf("close: %d %s", status, body)
	}
}

// ---- evidence ----

func TestEvidenceCapAndConcurrency(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	const attempts = 70
	var wg sync.WaitGroup
	var mu sync.Mutex
	created := 0
	for i := 0; i < attempts; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			status, _ := do(t, "POST", "/incidents/"+id+"/evidence", analyst1ID,
				map[string]any{"content": fmt.Sprintf("evidence %d", i)})
			if status == http.StatusCreated {
				mu.Lock()
				created++
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if created != 50 {
		t.Fatalf("created = %d, want exactly 50", created)
	}

	// The 51st sequential attempt is rejected too.
	status, body := do(t, "POST", "/incidents/"+id+"/evidence", analyst1ID,
		map[string]any{"content": "one too many"})
	if status != http.StatusUnprocessableEntity || errCode(t, body) != "evidence_limit" {
		t.Fatalf("expected evidence_limit 422, got %d %s", status, body)
	}

	// Sequences are dense and unique.
	_, body = do(t, "GET", "/incidents/"+id+"/evidence", analyst1ID, nil)
	items := mustArray(t, body)
	if len(items) != 50 {
		t.Fatalf("stored evidence = %d, want 50", len(items))
	}
	seen := map[float64]bool{}
	for _, it := range items {
		seq := it.(map[string]any)["seq"].(float64)
		if seen[seq] {
			t.Fatalf("duplicate seq %v", seq)
		}
		seen[seq] = true
	}
}

func TestEvidenceImmutableWithAppendOnlyNotes(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	status, body := do(t, "POST", "/incidents/"+id+"/evidence", analyst1ID,
		map[string]any{"content": "original log line"})
	if status != http.StatusCreated {
		t.Fatalf("add evidence: %d %s", status, body)
	}
	evID := decode(t, body)["id"].(string)

	// There is no endpoint to overwrite evidence.
	status, _ = do(t, "PUT", "/incidents/"+id+"/evidence", analyst1ID,
		map[string]any{"content": "tampered"})
	if status != http.StatusMethodNotAllowed && status != http.StatusNotFound {
		t.Fatalf("evidence must not be mutable, got %d", status)
	}

	// Corrections are appended as linked notes.
	status, body = do(t, "POST", "/evidence/"+evID+"/notes", analyst2ID,
		map[string]any{"content": "correction: timestamp was UTC+8"})
	if status != http.StatusCreated {
		t.Fatalf("add note: %d %s", status, body)
	}

	// Original content is untouched; export shows the note count.
	_, body = do(t, "GET", "/incidents/"+id+"/export", analyst1ID, nil)
	exp := decode(t, body)
	items := exp["evidence"].(map[string]any)["items"].([]any)
	first := items[0].(map[string]any)
	if first["excerpt"] != "original log line" {
		t.Fatalf("evidence content changed: %v", first["excerpt"])
	}
	if first["noteCount"].(float64) != 1 {
		t.Fatalf("noteCount = %v, want 1", first["noteCount"])
	}
}

// ---- reminders ----

// makeDueActionItem builds an incident with an action item due at the given time.
func makeDueActionItem(t *testing.T, dueAt time.Time) (incidentID, itemID string) {
	t.Helper()
	incidentID = createIncident(t, "P2")
	assign(t, incidentID, responder1)
	itemID = addActionItem(t, incidentID, responder1, dueAt)
	return incidentID, itemID
}

func reminderCount(t *testing.T, incidentID string) int {
	t.Helper()
	var n int
	if err := testPool.QueryRow(context.Background(), `
		SELECT count(*) FROM reminders r
		JOIN action_items ai ON ai.id = r.action_item_id
		WHERE ai.incident_id = $1`, incidentID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestReminderOncePerDueVersion(t *testing.T) {
	reset(t)
	incidentID, _ := makeDueActionItem(t, time.Now().Add(-time.Minute))
	s := reminder.NewScheduler(testPool, time.Minute)

	n, err := s.RunOnce(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("first run: n=%d err=%v, want 1", n, err)
	}
	// Same due version never reminds twice, however often we scan.
	for i := 0; i < 3; i++ {
		n, err = s.RunOnce(context.Background())
		if err != nil || n != 0 {
			t.Fatalf("run %d: n=%d err=%v, want 0", i, n, err)
		}
	}
	if got := reminderCount(t, incidentID); got != 1 {
		t.Fatalf("reminders = %d, want 1", got)
	}
}

func TestRescheduleSuppressesStaleReminder(t *testing.T) {
	reset(t)
	incidentID, itemID := makeDueActionItem(t, time.Now().Add(-time.Minute))
	s := reminder.NewScheduler(testPool, time.Minute)

	// Rescheduled to the future before the scheduler fires: no stale reminder.
	status, body := do(t, "POST", "/action-items/"+itemID+"/reschedule", responder1,
		map[string]any{"dueAt": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)})
	if status != http.StatusOK {
		t.Fatalf("reschedule: %d %s", status, body)
	}
	n, err := s.RunOnce(context.Background())
	if err != nil || n != 0 {
		t.Fatalf("run after reschedule: n=%d err=%v, want 0", n, err)
	}
	if got := reminderCount(t, incidentID); got != 0 {
		t.Fatalf("stale reminder was sent, reminders = %d", got)
	}

	// Rescheduled into the past: the new due version reminds exactly once.
	status, body = do(t, "POST", "/action-items/"+itemID+"/reschedule", responder1,
		map[string]any{"dueAt": time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)})
	if status != http.StatusOK {
		t.Fatalf("reschedule 2: %d %s", status, body)
	}
	n, err = s.RunOnce(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("run for new version: n=%d err=%v, want 1", n, err)
	}
	n, err = s.RunOnce(context.Background())
	if err != nil || n != 0 {
		t.Fatalf("rerun: n=%d err=%v, want 0", n, err)
	}
	if got := reminderCount(t, incidentID); got != 1 {
		t.Fatalf("reminders = %d, want 1", got)
	}
}

func TestConcurrentSchedulersDeliverOnce(t *testing.T) {
	reset(t)
	incidentID, _ := makeDueActionItem(t, time.Now().Add(-time.Minute))

	const n = 4
	var wg sync.WaitGroup
	totals := make([]int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			s := reminder.NewScheduler(testPool, time.Minute)
			sent, err := s.RunOnce(context.Background())
			if err != nil {
				t.Errorf("scheduler %d: %v", i, err)
			}
			totals[i] = sent
		}(i)
	}
	wg.Wait()
	total := 0
	for _, s := range totals {
		total += s
	}
	if total != 1 || reminderCount(t, incidentID) != 1 {
		t.Fatalf("concurrent schedulers sent %d reminders, want 1", total)
	}
}

func TestRestartCatchUp(t *testing.T) {
	reset(t)
	// Item comes due while the service is "down" (no scheduler running).
	incidentID, _ := makeDueActionItem(t, time.Now().Add(-time.Minute))

	// "Restart": a fresh scheduler picks up the overdue item on its first scan.
	s := reminder.NewScheduler(testPool, time.Minute)
	n, err := s.RunOnce(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("catch-up run: n=%d err=%v, want 1", n, err)
	}
	if got := reminderCount(t, incidentID); got != 1 {
		t.Fatalf("reminders = %d, want 1", got)
	}
}

// ---- authorization ----

func TestAuthorization(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	assign(t, id, responder1)

	cases := []struct {
		name   string
		method string
		path   string
		user   string
		body   any
		want   int
	}{
		{"no auth header", "GET", "/incidents/" + id, "", nil, http.StatusUnauthorized},
		{"unknown user", "GET", "/incidents/" + id, "00000000-0000-0000-0000-00000000ffff", nil, http.StatusUnauthorized},
		{"responder cannot triage", "POST", "/incidents/" + id + "/transitions", responder1,
			map[string]any{"toStatus": "triaged", "expectedVersion": 2, "requestId": "x1"}, http.StatusForbidden},
		{"admin cannot triage", "POST", "/incidents/" + id + "/transitions", adminID,
			map[string]any{"toStatus": "triaged", "expectedVersion": 2, "requestId": "x2"}, http.StatusForbidden},
		{"analyst cannot assign", "POST", "/incidents/" + id + "/assign", analyst1ID,
			map[string]any{"responderId": responder2}, http.StatusForbidden},
		{"responder cannot add evidence", "POST", "/incidents/" + id + "/evidence", responder1,
			map[string]any{"content": "x"}, http.StatusForbidden},
		{"admin cannot add evidence", "POST", "/incidents/" + id + "/evidence", adminID,
			map[string]any{"content": "x"}, http.StatusForbidden},
	}
	for _, c := range cases {
		status, body := do(t, c.method, c.path, c.user, c.body)
		if status != c.want {
			t.Errorf("%s: got %d want %d (%s)", c.name, status, c.want, body)
		}
	}

	// Case scope: a responder who is not the assignee cannot drive the case.
	status, body := transition(t, id, analyst1ID, "triaged", 2, "req-authz-triage")
	if status != http.StatusOK {
		t.Fatalf("triage: %d %s", status, body)
	}
	status, body = transition(t, id, responder2, "contained", 3, "req-authz-cont")
	if status != http.StatusForbidden {
		t.Fatalf("non-assignee responder: got %d want 403 (%s)", status, body)
	}
	status, body = transition(t, id, responder1, "contained", 3, "req-authz-cont2")
	if status != http.StatusOK {
		t.Fatalf("assignee responder: %d %s", status, body)
	}
}

// ---- metrics ----

func TestMetricsNullUntilPhaseCompletes(t *testing.T) {
	reset(t)
	id := createIncident(t, "P2")
	assign(t, id, responder1)

	_, body := do(t, "GET", "/incidents/"+id+"/export", analyst1ID, nil)
	metrics := decode(t, body)["metrics"].(map[string]any)
	if metrics["containmentDurationSeconds"] != nil || metrics["resolutionDurationSeconds"] != nil {
		t.Fatalf("durations must be null before the phases complete: %v", metrics)
	}

	status, body := transition(t, id, analyst1ID, "triaged", 2, "req-m1")
	if status != http.StatusOK {
		t.Fatalf("triage: %d %s", status, body)
	}
	status, body = transition(t, id, responder1, "contained", 3, "req-m2")
	if status != http.StatusOK {
		t.Fatalf("contained: %d %s", status, body)
	}
	_, body = do(t, "GET", "/incidents/"+id+"/export", analyst1ID, nil)
	metrics = decode(t, body)["metrics"].(map[string]any)
	if metrics["containmentDurationSeconds"] == nil {
		t.Fatal("containment duration should be set")
	}
	if metrics["resolutionDurationSeconds"] != nil {
		t.Fatal("resolution duration must stay null until close")
	}
}

func mustArray(t *testing.T, data []byte) []any {
	t.Helper()
	var v []any
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatalf("decode array %s: %v", data, err)
	}
	return v
}
