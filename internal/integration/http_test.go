package integration

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"communityvault/internal/content"
	httpserver "communityvault/internal/httpserver"
	"communityvault/internal/moderation"
	"communityvault/internal/rules"
	"communityvault/internal/testsupport"
	"communityvault/internal/view"
)

type api struct {
	t      *testing.T
	srv    http.Handler
	tokens map[string]string
}

func newAPI(t *testing.T) *api {
	t.Helper()
	pool := testsupport.Pool(t)
	testsupport.Reset(t, pool)
	rs := rules.New(pool)
	s := &api{
		t:   t,
		srv: httpserver.NewServer(pool, content.New(pool, rs), moderation.New(pool, rs, time.Hour), rs, view.New(pool)).Router(),
		tokens: map[string]string{
			"alice":   "token-alice",
			"bob":     "token-bob",
			"modTech": "token-mod-tech",
			"modLife": "token-mod-life",
			"admin":   "token-admin",
		},
	}
	return s
}

func (a *api) do(method, path, who string, body any) (int, map[string]any) {
	a.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	if who != "" {
		req.Header.Set("Authorization", "Bearer "+a.tokens[who])
	}
	rec := httptest.NewRecorder()
	a.srv.ServeHTTP(rec, req)
	out := map[string]any{}
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
	}
	return rec.Code, out
}

func num(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	default:
		return 0
	}
}

func TestHTTPAuthRequirement(t *testing.T) {
	a := newAPI(t)
	// Creating content anonymously and with a bogus token both fail.
	code, _ := a.do("POST", "/v1/contents/", "", map[string]any{"category_id": 1, "title": "x", "body": "y"})
	if code != http.StatusUnauthorized {
		t.Fatalf("anon create = %d", code)
	}
	req := httptest.NewRequest("POST", "/v1/contents/", bytes.NewReader([]byte(`{"category_id":1,"title":"x","body":"y"}`)))
	req.Header.Set("Authorization", "Bearer not-a-real-token")
	rec := httptest.NewRecorder()
	a.srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("bogus token = %d, want 401", rec.Code)
	}
	// Feed and categories remain public.
	if code, _ := a.do("GET", "/v1/feed", "", nil); code != 200 {
		t.Fatalf("feed = %d", code)
	}
	if code, _ := a.do("GET", "/v1/categories", "", nil); code != 200 {
		t.Fatalf("categories = %d", code)
	}
}

func TestHTTPFullModerationFlow(t *testing.T) {
	a := newAPI(t)

	// Alice drafts and submits.
	code, out := a.do("POST", "/v1/contents/", "alice", map[string]any{"category_id": 1, "title": "hi", "body": "clean text"})
	if code != 201 {
		t.Fatalf("create %d %v", code, out)
	}
	cid := num(out["content"].(map[string]any)["id"])

	if code, out = a.do("POST", "/v1/contents/"+itoaStr(cid)+"/submit", "alice", nil); code != 200 {
		t.Fatalf("submit %d %v", code, out)
	}

	// A member cannot touch the queue.
	if code, _ := a.do("POST", "/v1/moderation/claims", "bob", nil); code != http.StatusForbidden {
		t.Fatalf("member claim = %d", code)
	}
	// The life moderator cannot see the tech task; no task for them.
	if code, out := a.do("POST", "/v1/moderation/claims", "modLife", nil); code != http.StatusConflict {
		t.Fatalf("cross-category claim = %d %v", code, out)
	}

	// Tech moderator claims and approves.
	code, out = a.do("POST", "/v1/moderation/claims", "modTech", nil)
	if code != 201 {
		t.Fatalf("claim %d %v", code, out)
	}
	taskID := num(out["task"].(map[string]any)["id"])
	code, out = a.do("POST", "/v1/moderation/claims/"+itoaStr(taskID)+"/approve", "modTech", nil)
	if code != 200 {
		t.Fatalf("approve %d %v", code, out)
	}

	// Public feed now shows it.
	code, out = a.do("GET", "/v1/feed", "", nil)
	if code != 200 {
		t.Fatalf("feed %d", code)
	}
	items := out["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("feed len = %d", len(items))
	}
}

func TestHTTPSensitiveWordsAndReports(t *testing.T) {
	a := newAPI(t)

	// Submit is blocked by the seeded sensitive word.
	code, out := a.do("POST", "/v1/contents/", "alice", map[string]any{"category_id": 1, "title": "t", "body": "contains spamword yes"})
	if code != 201 {
		t.Fatalf("create %d", code)
	}
	cid := num(out["content"].(map[string]any)["id"])
	if code, out = a.do("POST", "/v1/contents/"+itoaStr(cid)+"/submit", "alice", nil); code != http.StatusUnprocessableEntity {
		t.Fatalf("submit blocked = %d %v", code, out)
	}
	if out["error"] != "sensitive_words" {
		t.Fatalf("error code = %v", out["error"])
	}

	// Rewrite cleanly, submit, then Bob reports twice.
	if code, _ := a.do("PUT", "/v1/contents/"+itoaStr(cid), "alice", map[string]any{"body": "totally clean now", "reason": "reworded"}); code != 200 {
		t.Fatalf("edit = %d", code)
	}
	if code, _ := a.do("POST", "/v1/contents/"+itoaStr(cid)+"/submit", "alice", nil); code != 200 {
		t.Fatalf("resubmit = %d", code)
	}
	if code, _ := a.do("POST", "/v1/contents/"+itoaStr(cid)+"/reports", "bob", map[string]any{"reason": "doubt"}); code != 201 {
		t.Fatalf("report1 = %d", code)
	}
	if code, out := a.do("POST", "/v1/contents/"+itoaStr(cid)+"/reports", "bob", map[string]any{"reason": "again"}); code != http.StatusConflict || out["error"] != "duplicate_report" {
		t.Fatalf("report2 = %d %v", code, out)
	}
	// Alice (author) cannot read who reported.
	if code, _ := a.do("GET", "/v1/contents/"+itoaStr(cid)+"/reports", "alice", nil); code != http.StatusForbidden {
		t.Fatalf("author reports list = %d", code)
	}
	// Tech moderator can.
	if code, out := a.do("GET", "/v1/contents/"+itoaStr(cid)+"/reports", "modTech", nil); code != 200 || len(out["reports"].([]any)) != 1 {
		t.Fatalf("mod reports = %d %v", code, out)
	}
}

func TestHTTPRuleAdminOnly(t *testing.T) {
	a := newAPI(t)
	if code, _ := a.do("GET", "/v1/rules/active", "modTech", nil); code != 200 {
		t.Fatalf("mod read rules = %d", code)
	}
	if code, _ := a.do("POST", "/v1/rules/", "modTech", map[string]any{"words": []string{"x"}}); code != http.StatusForbidden {
		t.Fatalf("mod create rule = %d", code)
	}
	code, out := a.do("POST", "/v1/rules/", "admin", map[string]any{"note": "v2", "words": []string{"banned2"}})
	if code != 201 {
		t.Fatalf("admin create rule = %d %v", code, out)
	}
	rid := num(out["rule"].(map[string]any)["id"])
	if code, out := a.do("POST", "/v1/rules/"+itoaStr(rid)+"/activate", "admin", nil); code != 200 {
		t.Fatalf("activate = %d %v", code, out)
	}
	if code, out := a.do("GET", "/v1/rules/active", "modTech", nil); code != 200 {
		t.Fatalf("active after switch = %d %v", code, out)
	}
}

func itoaStr(v int64) string { return strconv.FormatInt(v, 10) }
