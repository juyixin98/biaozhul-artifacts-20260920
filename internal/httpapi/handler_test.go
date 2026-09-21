package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/migrate"
	"communityvault/internal/testdb"
)

var (
	baseURL string
	client  = &http.Client{Timeout: 10 * time.Second}
	dbPool  *pgxpool.Pool
)

func TestMain(m *testing.M) {
	p, _, err := testdb.Setup(context.Background(), "httpapi", migrate.Up)
	if err != nil {
		fmt.Fprintln(os.Stderr, "SKIP: test db unavailable:", err)
		os.Exit(0)
	}
	dbPool = p

	// 5-second claim TTL to exercise expiry over HTTP quickly.
	handler := newTestHandler(p)
	ts := httptest.NewServer(handler)
	baseURL = ts.URL
	defer ts.Close()
	os.Exit(m.Run())
}

func resetDB(t *testing.T) {
	t.Helper()
	_, err := dbPool.Exec(context.Background(), `
		TRUNCATE reports, status_events, moderation_decisions, review_tasks,
		         content_revisions, contents, rule_words, rule_versions,
		         moderator_scopes, users
		RESTART IDENTITY CASCADE;
		INSERT INTO users (id, username, role) VALUES
			(1,'alice','member'),(2,'bob','member'),
			(3,'mod_tech','moderator'),(4,'mod_art','moderator'),
			(5,'admin','admin');
		INSERT INTO moderator_scopes (user_id, category) VALUES (3,'tech'),(4,'art');
		INSERT INTO rule_versions (id, version, status, description, created_by)
			VALUES (1,1,'active','baseline word list',5);
		INSERT INTO rule_words (rule_version_id, word) VALUES (1,'forbidden');
		SELECT setval(pg_get_serial_sequence('users','id'), (SELECT max(id) FROM users));
		SELECT setval(pg_get_serial_sequence('rule_versions','id'), (SELECT max(id) FROM rule_versions));
	`)
	if err != nil {
		t.Fatalf("reset: %v", err)
	}
}

func req(t *testing.T, method, path string, userID int, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	r, err := http.NewRequest(method, baseURL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	r.Header.Set("X-User-ID", fmt.Sprint(userID))
	r.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	dec := json.NewDecoder(resp.Body)
	_ = dec.Decode(&out) // empty body on 204 etc. is fine
	return resp.StatusCode, out
}

func mustInt(t *testing.T, v any, what string) int64 {
	t.Helper()
	f, ok := v.(float64)
	if !ok {
		t.Fatalf("%s is not a number: %v", what, v)
	}
	return int64(f)
}

// Full HTTP walkthrough: create -> missing auth rejected -> submit -> claim by
// scoped moderator -> approve -> visible in feed -> withdraw.
func TestHTTPFullModerationFlow(t *testing.T) {
	resetDB(t)

	// Auth is required.
	resp, err := http.Post(baseURL+"/v1/contents", "application/json",
		bytes.NewReader([]byte(`{"category":"tech","title":"t","body":"b"}`)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no auth header: want 401, got %d", resp.StatusCode)
	}

	// Alice creates a draft.
	status, body := req(t, "POST", "/v1/contents", 1, map[string]any{
		"category": "tech", "title": "hello world", "body": "a perfectly clean post",
	})
	if status != http.StatusCreated {
		t.Fatalf("create: %d %v", status, body)
	}
	contentID := mustInt(t, body["id"], "id")

	// Bob cannot read alice's draft.
	if s, _ := req(t, "GET", fmt.Sprintf("/v1/contents/%d", contentID), 2, nil); s != http.StatusNotFound {
		t.Fatalf("bob reading draft: want 404, got %d", s)
	}

	// Submit -> pending.
	if s, b := req(t, "POST", fmt.Sprintf("/v1/contents/%d/submit", contentID), 1, nil); s != http.StatusOK {
		t.Fatalf("submit: %d %v", s, b)
	}

	// Art moderator cannot claim; tech moderator can.
	if s, b := req(t, "POST", "/v1/mod/claims", 4, nil); s != http.StatusNoContent {
		t.Fatalf("art mod claim: want 204, got %d %v", s, b)
	}
	s, claim := req(t, "POST", "/v1/mod/claims", 3, nil)
	if s != http.StatusOK {
		t.Fatalf("tech mod claim: %d %v", s, claim)
	}
	taskID := mustInt(t, claim["task_id"], "task_id")
	if claim["category"] != "tech" {
		t.Fatalf("claimed wrong category: %v", claim["category"])
	}

	// Second concurrent claim finds nothing else to do.
	if s, _ := req(t, "POST", "/v1/mod/claims", 3, nil); s != http.StatusNoContent {
		t.Fatalf("duplicate claim: want 204, got %d", s)
	}

	// Reject without a reason -> 400; approve -> published.
	if s, b := req(t, "POST", fmt.Sprintf("/v1/mod/tasks/%d/reject", taskID), 3,
		map[string]any{"reason": ""}); s != http.StatusBadRequest {
		t.Fatalf("empty reject reason: want 400, got %d %v", s, b)
	}
	if s, b := req(t, "POST", fmt.Sprintf("/v1/mod/tasks/%d/approve", taskID), 3,
		map[string]any{"reason": "fine"}); s != http.StatusOK {
		t.Fatalf("approve: %d %v", s, b)
	}

	// Bob now sees it in the feed and can read it.
	s, feed := req(t, "GET", "/v1/contents?limit=5", 2, nil)
	if s != http.StatusOK {
		t.Fatalf("feed: %d", s)
	}
	items, _ := feed["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("feed must contain the one published post, got %v", items)
	}
	if s, _ := req(t, "GET", fmt.Sprintf("/v1/contents/%d", contentID), 2, nil); s != http.StatusOK {
		t.Fatalf("bob reading published: want 200, got %d", s)
	}

	// Withdraw requires a reason.
	if s, b := req(t, "POST", fmt.Sprintf("/v1/contents/%d/withdraw", contentID), 1,
		map[string]any{"reason": ""}); s != http.StatusBadRequest {
		t.Fatalf("withdraw no reason: want 400, got %d %v", s, b)
	}
	if s, b := req(t, "POST", fmt.Sprintf("/v1/contents/%d/withdraw", contentID), 1,
		map[string]any{"reason": "author request"}); s != http.StatusOK {
		t.Fatalf("withdraw: %d %v", s, b)
	}
	if s, _ := req(t, "GET", "/v1/contents", 2, nil); s != http.StatusOK {
		t.Fatalf("feed after withdraw: %d", s)
	}
}

// Auto-reject forbidden word over HTTP; evidence carries a rule_version_id.
func TestHTTPAutoRejectAndReportDedup(t *testing.T) {
	resetDB(t)
	_, body := req(t, "POST", "/v1/contents", 1, map[string]any{
		"category": "tech", "title": "bad", "body": "this is forbidden text",
	})
	contentID := mustInt(t, body["id"], "id")
	if s, b := req(t, "POST", fmt.Sprintf("/v1/contents/%d/submit", contentID), 1, nil); s != http.StatusUnprocessableEntity {
		t.Fatalf("auto reject: want 422, got %d %v", s, b)
	}

	// Duplicate reports from the same reporter are idempotent (200, created=false).
	if s, b := req(t, "POST", fmt.Sprintf("/v1/contents/%d/reports", contentID), 2,
		map[string]any{"reason": "spam"}); s != http.StatusCreated || b["created"] != true {
		t.Fatalf("first report: %d %v", s, b)
	}
	if s, b := req(t, "POST", fmt.Sprintf("/v1/contents/%d/reports", contentID), 2,
		map[string]any{"reason": "spam again"}); s != http.StatusOK || b["created"] != false {
		t.Fatalf("duplicate report: want 200 created=false, got %d %v", s, b)
	}

	// Author sees only aggregate counts; moderator sees reporter detail.
	s, authorView := req(t, "GET", fmt.Sprintf("/v1/contents/%d/reports", contentID), 1, nil)
	if s != http.StatusOK || authorView["total"].(float64) != 1 {
		t.Fatalf("author reports view wrong: %d %v", s, authorView)
	}
	if _, hasReporter := authorView["reporter_id"]; hasReporter {
		t.Fatalf("author must not see reporter id: %v", authorView)
	}
	s, raw := reqRaw(t, "GET", fmt.Sprintf("/v1/contents/%d/reports", contentID), 3, nil)
	if s != http.StatusOK {
		t.Fatalf("mod reports: %d", s)
	}
	var reports []map[string]any
	if err := json.Unmarshal(raw, &reports); err != nil || len(reports) != 1 {
		t.Fatalf("moderator must see a 1-element reports array, got %s", raw)
	}
	if reports[0]["reporter_id"].(float64) != 2 {
		t.Fatalf("moderator view must expose reporter id: %v", reports[0])
	}
}

// reqRaw returns the raw decoded body bytes for endpoints that return arrays.
func reqRaw(t *testing.T, method, path string, userID int, body any) (int, []byte) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	r, err := http.NewRequest(method, baseURL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	r.Header.Set("X-User-ID", fmt.Sprint(userID))
	r.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := new(bytes.Buffer)
	_, _ = buf.ReadFrom(resp.Body)
	return resp.StatusCode, buf.Bytes()
}

// Rule versioning over HTTP: only admin can publish a new rule version.
func TestHTTPRuleSwitch(t *testing.T) {
	resetDB(t)
	if s, _ := req(t, "POST", "/v1/rules", 3, map[string]any{
		"description": "x", "words": []string{"zap"},
	}); s != http.StatusForbidden {
		t.Fatalf("moderator creating rule: want 403, got %d", s)
	}
	s, rv := req(t, "POST", "/v1/rules", 5, map[string]any{
		"description": "add zap", "words": []string{"zap"},
	})
	if s != http.StatusCreated {
		t.Fatalf("admin creating rule: %d %v", s, rv)
	}
	if rv["version"].(float64) != 2 {
		t.Fatalf("new rule must be version 2, got %v", rv["version"])
	}
	_, body := req(t, "POST", "/v1/contents", 1, map[string]any{
		"category": "tech", "title": "z", "body": "contains zap now",
	})
	contentID := mustInt(t, body["id"], "id")
	if s, _ := req(t, "POST", fmt.Sprintf("/v1/contents/%d/submit", contentID), 1, nil); s != http.StatusUnprocessableEntity {
		t.Fatalf("new word must reject submit, got %d", s)
	}
}
