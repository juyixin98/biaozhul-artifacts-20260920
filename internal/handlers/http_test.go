package handlers_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"communitygov/internal/database/dbtest"
	"communitygov/internal/handlers"
	"communitygov/internal/services"
)

type env struct {
	t      *testing.T
	srv    *httptest.Server
	tokens map[string]string
}

func setup(t *testing.T) *env {
	t.Helper()
	store := dbtest.NewDB(t)
	users := services.NewUserService(store)
	posts := services.NewPostService(store)
	members := services.NewMembershipService(store)
	reports := services.NewReportService(store, posts)
	courses := services.NewCourseService(store)
	h := &handlers.Handlers{Users: users, Posts: posts, Memberships: members,
		Reports: reports, Courses: courses}
	r := handlers.NewRouter(handlers.RouterDeps{
		Store: store, Handlers: h, JWTSecret: "test-secret",
		JWTTTL: time.Hour,
	})
	return &env{t: t, srv: httptest.NewServer(r), tokens: map[string]string{}}
}

func (e *env) register(role, email string) {
	e.t.Helper()
	body := map[string]string{"email": email, "name": email, "password": "pw123456", "role": role}
	if code, _ := e.do("POST", "/api/register", nil, body, ""); code != http.StatusCreated {
		e.t.Fatalf("register %s: code %d", email, code)
	}
	code, resp := e.do("POST", "/api/login", nil,
		map[string]string{"email": email, "password": "pw123456"}, "")
	if code != http.StatusOK {
		e.t.Fatalf("login %s: code %d", email, code)
	}
	var lr struct {
		Token string `json:"token"`
	}
	_ = json.Unmarshal(resp, &lr)
	e.tokens[email] = lr.Token
}

func (e *env) do(method, path string, headers map[string]string, body any, token string) (int, []byte) {
	e.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, e.srv.URL+path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

func (e *env) token(email string) string {
	tok, ok := e.tokens[email]
	if !ok {
		e.t.Fatalf("no token for %s", email)
	}
	return tok
}

func (e *env) meID(token string) int64 {
	_, b := e.do("GET", "/api/me", nil, nil, token)
	return int64(decodeBody(e.t, b)["id"].(float64))
}

func decodeBody(t *testing.T, data []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("decode %s: %v", string(data), err)
	}
	return m
}

func pathf(format string, args ...any) string { return fmt.Sprintf(format, args...) }

// TestReviewerCannotRecordPayment: separation of duties — a reviewer can
// moderate content but must get 403 on payment registration.
func TestReviewerCannotRecordPayment(t *testing.T) {
	e := setup(t)
	e.register("admin", "a@x.io")
	e.register("reviewer", "r@x.io")
	e.register("member", "u@x.io")
	admin, rev, member := e.token("a@x.io"), e.token("r@x.io"), e.token("u@x.io")

	_, b := e.do("POST", "/api/communities", nil, map[string]string{"name": "C"}, admin)
	cid := int64(decodeBody(t, b)["id"].(float64))
	revID := e.meID(rev)
	if code, _ := e.do("POST", pathf("/api/communities/%d/reviewers", cid), nil,
		map[string]int64{"user_id": revID}, admin); code != 204 {
		t.Fatalf("add reviewer: %d", code)
	}
	_, tb := e.do("POST", pathf("/api/communities/%d/tiers", cid), nil,
		map[string]any{"level": 1, "name": "L", "price_cents": 100, "duration_days": 30}, admin)
	tierID := int64(decodeBody(t, tb)["id"].(float64))
	memberID := e.meID(member)

	payBody := map[string]any{
		"request_id": "x-1", "user_id": memberID, "tier_id": tierID,
		"amount_cents": 100, "extend_days": 30,
	}
	if code, b := e.do("POST", pathf("/api/communities/%d/payments", cid),
		map[string]string{"X-Request-Id": "x-1"}, payBody, rev); code != 403 {
		t.Fatalf("reviewer payment must be 403, got %d %s", code, b)
	}
	if code, _ := e.do("POST", pathf("/api/communities/%d/payments", cid),
		map[string]string{"X-Request-Id": "x-1"}, payBody, member); code != 403 {
		t.Fatalf("member payment must be 403, got %d", code)
	}
	if code, b := e.do("POST", pathf("/api/communities/%d/payments", cid),
		map[string]string{"X-Request-Id": "pay-1"}, payBody, admin); code != 201 {
		t.Fatalf("admin payment must be 201, got %d %s", code, b)
	}
}

// TestMemberCannotSelfReview: an author approving their own post is forbidden;
// the assigned reviewer succeeds.
func TestMemberCannotSelfReview(t *testing.T) {
	e := setup(t)
	e.register("admin", "a2@x.io")
	e.register("reviewer", "r2@x.io")
	e.register("member", "u2@x.io")
	admin, rev, author := e.token("a2@x.io"), e.token("r2@x.io"), e.token("u2@x.io")

	_, cb := e.do("POST", "/api/communities", nil, map[string]string{"name": "C"}, admin)
	cid := int64(decodeBody(t, cb)["id"].(float64))
	e.do("POST", pathf("/api/communities/%d/reviewers", cid), nil,
		map[string]int64{"user_id": e.meID(rev)}, admin)

	_, pb := e.do("POST", pathf("/api/communities/%d/posts", cid), nil,
		map[string]any{"title": "T", "body": "secret", "required_tier_level": 1}, author)
	postID := int64(decodeBody(t, pb)["post"].(map[string]any)["id"].(float64))
	versionID := int64(decodeBody(t, pb)["version"].(map[string]any)["id"].(float64))
	if code, _ := e.do("POST", pathf("/api/communities/%d/posts/%d/submit", cid, postID),
		nil, map[string]any{}, author); code != 200 {
		t.Fatalf("submit: %d", code)
	}
	if code, b := e.do("POST", pathf("/api/communities/%d/posts/%d/review", cid, postID), nil,
		map[string]any{"version_id": versionID, "approve": true}, author); code != 403 {
		t.Fatalf("self review must be 403, got %d %s", code, b)
	}
	if code, b := e.do("POST", pathf("/api/communities/%d/posts/%d/review", cid, postID), nil,
		map[string]any{"version_id": versionID, "approve": true, "reason": "ok"}, rev); code != 200 {
		t.Fatalf("reviewer approve must be 200, got %d %s", code, b)
	}
}

// TestContentIsolationBodyAttachmentExport: an unprivileged viewer and a
// cross-community path are denied on body, attachment download and export — all
// three surfaces must agree; a paid member passes all three.
func TestContentIsolationBodyAttachmentExport(t *testing.T) {
	e := setup(t)
	e.register("admin", "a3@x.io")
	e.register("reviewer", "r3@x.io")
	e.register("member", "author3@x.io")
	e.register("member", "paid3@x.io")
	e.register("member", "poor3@x.io")
	admin := e.token("a3@x.io")
	rev := e.token("r3@x.io")
	author := e.token("author3@x.io")
	paid := e.token("paid3@x.io")
	poor := e.token("poor3@x.io")

	_, cb := e.do("POST", "/api/communities", nil, map[string]string{"name": "C1"}, admin)
	c1 := int64(decodeBody(t, cb)["id"].(float64))
	_, cb2 := e.do("POST", "/api/communities", nil, map[string]string{"name": "C2"}, admin)
	c2 := int64(decodeBody(t, cb2)["id"].(float64))
	e.do("POST", pathf("/api/communities/%d/reviewers", c1), nil,
		map[string]int64{"user_id": e.meID(rev)}, admin)

	_, tb := e.do("POST", pathf("/api/communities/%d/tiers", c1), nil,
		map[string]any{"level": 2, "name": "L2", "price_cents": 200, "duration_days": 1}, admin)
	tierID := int64(decodeBody(t, tb)["id"].(float64))

	_, pb := e.do("POST", pathf("/api/communities/%d/posts", c1), nil,
		map[string]any{"title": "T", "body": "vip body", "required_tier_level": 2}, author)
	postID := int64(decodeBody(t, pb)["post"].(map[string]any)["id"].(float64))
	versionID := int64(decodeBody(t, pb)["version"].(map[string]any)["id"].(float64))
	e.do("POST", pathf("/api/communities/%d/posts/%d/submit", c1, postID), nil, struct{}{}, author)
	if code, b := e.do("POST", pathf("/api/communities/%d/posts/%d/review", c1, postID), nil,
		map[string]any{"version_id": versionID, "approve": true}, rev); code != 200 {
		t.Fatalf("approve: %d %s", code, b)
	}

	attID := uploadAttachment(t, e, c1, versionID, author)

	if code, b := e.do("POST", pathf("/api/communities/%d/payments", c1),
		map[string]string{"X-Request-Id": "pay-paid"},
		map[string]any{"request_id": "pay-paid", "user_id": e.meID(paid), "tier_id": tierID,
			"amount_cents": 200, "extend_days": 1}, admin); code != 201 {
		t.Fatalf("pay paid: %d %s", code, b)
	}

	bodyPath := pathf("/api/communities/%d/versions/%d", c1, versionID)
	exportPath := pathf("/api/communities/%d/versions/%d/export", c1, versionID)
	attPath := pathf("/api/communities/%d/attachments/%d", c1, attID)
	for _, p := range []string{bodyPath, exportPath, attPath} {
		if code, _ := e.do("GET", p, nil, nil, poor); code != 403 {
			t.Fatalf("non-member GET %s must be 403, got %d", p, code)
		}
	}
	if code, _ := e.do("GET", pathf("/api/communities/%d/versions/%d", c2, versionID),
		nil, nil, paid); code != 404 {
		t.Fatalf("cross-community read must be 404, got %d", code)
	}
	for _, p := range []string{bodyPath, exportPath, attPath} {
		if code, b := e.do("GET", p, nil, nil, paid); code != 200 {
			t.Fatalf("paid member GET %s must be 200, got %d %s", p, code, b)
		}
	}
}

// TestUnauthenticatedRejected: protected routes return 401 without a token.
func TestUnauthenticatedRejected(t *testing.T) {
	e := setup(t)
	if code, _ := e.do("GET", "/api/communities", nil, nil, ""); code != 401 {
		t.Fatalf("expected 401, got %d", code)
	}
}

func uploadAttachment(t *testing.T, e *env, communityID, versionID int64, token string) int64 {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, _ := mw.CreateFormFile("file", "notes.txt")
	_, _ = io.WriteString(fw, "attachment-bytes")
	mw.Close()

	req, _ := http.NewRequest("POST",
		e.srv.URL+pathf("/api/communities/%d/versions/%d/attachments", communityID, versionID), &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 201 {
		t.Fatalf("upload: %d %s", resp.StatusCode, data)
	}
	return int64(decodeBody(t, data)["id"].(float64))
}
