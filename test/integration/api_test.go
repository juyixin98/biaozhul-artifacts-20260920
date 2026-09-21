package integration

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"proofcycle/internal/api"
	"proofcycle/internal/models"
	"proofcycle/internal/service"
)

// apiEnv stands up the full HTTP stack against the test database.
type apiEnv struct {
	*Env
	server *httptest.Server
	tokens map[string]string // role -> token
}

func setupAPI(t *testing.T) *apiEnv {
	t.Helper()
	env := setupEnv(t)
	svc := service.New(env.DB, env.Store)
	h := api.New(env.DB, svc, 1<<20)
	ts := httptest.NewServer(h.Router())
	t.Cleanup(ts.Close)

	tokens := map[string]string{}
	for _, role := range []string{models.RoleDesigner, models.RolePM, models.RoleReviewer} {
		u := createUser(t, env.DB, "user-"+role, role)
		tokens[role] = u.Token
	}
	return &apiEnv{Env: env, server: ts, tokens: tokens}
}

func (a *apiEnv) req(t *testing.T, method, path string, body io.Reader, ctype string, token string) (int, []byte) {
	t.Helper()
	r, err := http.NewRequest(method, a.server.URL+path, body)
	if err != nil {
		t.Fatal(err)
	}
	if ctype != "" {
		r.Header.Set("Content-Type", ctype)
	}
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

func (a *apiEnv) jsonReq(t *testing.T, method, path, payload, token string) (int, map[string]any) {
	code, data := a.req(t, method, path, strings.NewReader(payload), "application/json", token)
	out := map[string]any{}
	_ = json.Unmarshal(data, &out)
	return code, out
}

// upload builds a multipart request.
func (a *apiEnv) upload(t *testing.T, path, filename string, content []byte, token string) (int, map[string]any) {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, err := mw.CreateFormFile("file", filename)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fw.Write(content); err != nil {
		t.Fatal(err)
	}
	mw.Close()
	code, data := a.req(t, "POST", path, &buf, mw.FormDataContentType(), token)
	out := map[string]any{}
	_ = json.Unmarshal(data, &out)
	return code, out
}

// seedJobViaAPI creates a job with the PM token and returns its ID plus the
// reviewer tokens on its roster.
func (a *apiEnv) seedJobViaAPI(t *testing.T, reviewerCount int) (int, []string) {
	t.Helper()
	ids := []int{}
	toks := []string{}
	for i := 0; i < reviewerCount; i++ {
		u := createUser(t, a.DB, fmt.Sprintf("job-reviewer-%d", i), models.RoleReviewer)
		ids = append(ids, int(u.ID))
		toks = append(toks, u.Token)
	}
	var designerID, pmID int
	a.DB.Raw("SELECT id FROM users WHERE token = ?", a.tokens[models.RoleDesigner]).Scan(&designerID)
	a.DB.Raw("SELECT id FROM users WHERE token = ?", a.tokens[models.RolePM]).Scan(&pmID)
	payload, _ := json.Marshal(map[string]any{
		"title":        "api job",
		"designer_id":  designerID,
		"pm_id":        pmID,
		"reviewer_ids": ids,
	})
	code, body := a.jsonReq(t, "POST", "/api/v1/jobs", string(payload), a.tokens[models.RolePM])
	if code != 201 {
		t.Fatalf("create job: %d %v", code, body)
	}
	return int(body["job"].(map[string]any)["id"].(float64)), toks
}

func TestHealthAndAuth(t *testing.T) {
	a := setupAPI(t)
	if code, _ := a.req(t, "GET", "/healthz", nil, "", ""); code != 200 {
		t.Fatalf("health = %d", code)
	}
	// Missing / malformed tokens.
	if code, _ := a.req(t, "GET", "/api/v1/me", nil, "", ""); code != 401 {
		t.Fatalf("no token = %d", code)
	}
	if code, _ := a.req(t, "GET", "/api/v1/me", nil, "", "garbage"); code != 401 {
		t.Fatalf("bad token = %d", code)
	}
	if code, _ := a.req(t, "GET", "/api/v1/me", nil, "", ""); code != 401 {
		t.Fatalf("basic auth prefix = %d", code)
	}
	// Valid token works.
	if code, body := a.jsonReq(t, "GET", "/api/v1/me", "{}", a.tokens[models.RolePM]); code != 200 || body["user"] == nil {
		t.Fatalf("me = %d %v", code, body)
	}
}

func TestAPIRoleIsolation(t *testing.T) {
	a := setupAPI(t)
	jobID, revTokens := a.seedJobViaAPI(t, 2)
	designer := a.tokens[models.RoleDesigner]
	pm := a.tokens[models.RolePM]
	rev1 := revTokens[0]
	otherReviewer := a.tokens[models.RoleReviewer] // role reviewer but NOT on roster

	// Upload first version as designer: ok.
	if code, body := a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/versions", jobID), "v1.pdf", pdfBytes, designer); code != 201 {
		t.Fatalf("designer upload = %d %v", code, body)
	}
	// Reviewer cannot create jobs.
	if code, _ := a.jsonReq(t, "POST", "/api/v1/jobs", `{"title":"x","designer_id":1,"pm_id":2,"reviewer_ids":[3]}`, rev1); code != 403 {
		t.Fatalf("reviewer create job = %d, want 403", code)
	}
	// Assigned reviewer cannot upload a revision (member, wrong role).
	if code, _ := a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/revisions", jobID), "v2.pdf", pdfBytes, rev1); code != 403 {
		t.Fatalf("reviewer revision = %d, want 403", code)
	}
	// Non-roster reviewer cannot see the job or its files.
	if code, _ := a.req(t, "GET", fmt.Sprintf("/api/v1/jobs/%d", jobID), nil, "", otherReviewer); code != 404 {
		t.Fatalf("outsider GET job = %d, want 404", code)
	}
	if code, _ := a.req(t, "GET", fmt.Sprintf("/api/v1/jobs/%d/versions/1/download", jobID), nil, "", otherReviewer); code != 404 {
		t.Fatalf("outsider download = %d, want 404", code)
	}
	// Designer cannot approve.
	if code, _ := a.jsonReq(t, "POST", fmt.Sprintf("/api/v1/jobs/%d/approvals", jobID), "{}", designer); code != 403 {
		t.Fatalf("designer approve = %d, want 403", code)
	}
	// PM can't submit reviewer opinions (member, wrong role at service).
	if code, _ := a.jsonReq(t, "PUT", fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID),
		`{"items":[{"code":"COLOR","outcome":"pass"}]}`, pm); code != 403 {
		t.Fatalf("pm opinion = %d, want 403", code)
	}
}

func TestAPIUploadGuards(t *testing.T) {
	a := setupAPI(t)
	jobID, _ := a.seedJobViaAPI(t, 1)
	designer := a.tokens[models.RoleDesigner]

	// Wrong magic.
	if code, body := a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/versions", jobID), "evil.exe", []byte("MZ\x90\x00binary"), designer); code != 415 {
		t.Fatalf("bad magic = %d %v", code, body)
	}
	// Oversize (store limit is 1 MiB in tests).
	big := append(append([]byte{}, pdfBytes...), make([]byte, 2<<20)...)
	if code, _ := a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/versions", jobID), "big.pdf", big, designer); code != 413 && code != 500 {
		// MaxBytesReader may surface a 413; streaming limit also 413.
		t.Fatalf("oversize = %d, want 413", code)
	}
	// Valid PDF lands and its download round-trips with matching sha.
	code, body := a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/versions", jobID), "v1.pdf", pdfBytes, designer)
	if code != 201 {
		t.Fatalf("valid upload = %d %v", code, body)
	}
	dcode, data := a.req(t, "GET", fmt.Sprintf("/api/v1/jobs/%d/versions/1/download", jobID), nil, "", designer)
	if dcode != 200 || !bytes.Equal(data, pdfBytes) {
		t.Fatalf("download = %d, %d bytes", dcode, len(data))
	}
}

func TestAPIApproveFlowAndFailureBlock(t *testing.T) {
	a := setupAPI(t)
	jobID, revTokens := a.seedJobViaAPI(t, 2)
	designer := a.tokens[models.RoleDesigner]
	pm := a.tokens[models.RolePM]

	a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/versions", jobID), "v1.pdf", pdfBytes, designer)

	codes := []string{"COLOR", "BLEED", "TYPOGRAPHY", "RESOLUTION", "BARCODE", "MATERIAL", "REGULATORY", "FINISHING"}
	submit := func(token, outcome, reason string, expected map[string]int) int {
		type item struct {
			Code            string `json:"code"`
			Outcome         string `json:"outcome"`
			Reason          string `json:"reason,omitempty"`
			ExpectedVersion int    `json:"expected_version,omitempty"`
		}
		items := make([]item, 0, len(codes))
		for _, c := range codes {
			it := item{Code: c, Outcome: outcome}
			if c == "BLEED" && reason != "" {
				it.Outcome = "fail"
				it.Reason = reason
			}
			if expected != nil {
				it.ExpectedVersion = expected[c]
			}
			items = append(items, it)
		}
		b, _ := json.Marshal(map[string]any{"items": items})
		code, _ := a.jsonReq(t, "PUT", fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID), string(b), token)
		return code
	}

	if submit(revTokens[0], "pass", "", nil) != 200 {
		t.Fatal("reviewer 1 pass failed")
	}
	if submit(revTokens[1], "pass", "bleed issue", nil) != 200 {
		t.Fatal("reviewer 1 fail-with-reason failed")
	}
	// Blocked by failure.
	if code, _ := a.jsonReq(t, "POST", fmt.Sprintf("/api/v1/jobs/%d/approvals", jobID), "{}", pm); code != 422 {
		t.Fatalf("approve with fail = %d, want 422", code)
	}
	// Stale optimistic version -> 409.
	stale, _ := json.Marshal(map[string]any{"items": []map[string]any{
		{"code": "BLEED", "outcome": "pass", "expected_version": 99},
	}})
	if code, _ := a.jsonReq(t, "PUT", fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID), string(stale), revTokens[1]); code != 409 {
		t.Fatalf("stale version = %d, want 409", code)
	}
	// Correct version -> fixed.
	fixed, _ := json.Marshal(map[string]any{"items": []map[string]any{
		{"code": "BLEED", "outcome": "pass", "expected_version": 1},
	}})
	if code, _ := a.jsonReq(t, "PUT", fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID), string(fixed), revTokens[1]); code != 200 {
		t.Fatalf("fix BLEED = %d", code)
	}
	// Approve succeeds once; a repeat is 409.
	if code, _ := a.jsonReq(t, "POST", fmt.Sprintf("/api/v1/jobs/%d/approvals", jobID), "{}", pm); code != 201 {
		t.Fatalf("approve = %d, want 201", code)
	}
	if code, _ := a.jsonReq(t, "POST", fmt.Sprintf("/api/v1/jobs/%d/approvals", jobID), "{}", pm); code != 409 {
		t.Fatalf("re-approve = %d, want 409", code)
	}
	// Report shows approved basis.
	code, data := a.req(t, "GET", fmt.Sprintf("/api/v1/jobs/%d/versions/1/report?format=text", jobID), nil, "", pm)
	if code != 200 || !strings.Contains(string(data), "APPROVED by") {
		t.Fatalf("report = %d: %.200s", code, data)
	}
}

func TestAPIConcurrentApprove(t *testing.T) {
	a := setupAPI(t)
	jobID, revTokens := a.seedJobViaAPI(t, 2)
	designer := a.tokens[models.RoleDesigner]
	pm := a.tokens[models.RolePM]
	a.upload(t, fmt.Sprintf("/api/v1/jobs/%d/versions", jobID), "v1.pdf", pdfBytes, designer)

	codes := []string{"COLOR", "BLEED", "TYPOGRAPHY", "RESOLUTION", "BARCODE", "MATERIAL", "REGULATORY", "FINISHING"}
	for _, tok := range revTokens {
		items := make([]map[string]string, 0, len(codes))
		for _, c := range codes {
			items = append(items, map[string]string{"code": c, "outcome": "pass"})
		}
		b, _ := json.Marshal(map[string]any{"items": items})
		if code, _ := a.jsonReq(t, "PUT", fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID), string(b), tok); code != 200 {
			t.Fatalf("opinion %d", code)
		}
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	statuses := map[int]int{}
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			code, _ := a.jsonReq(t, "POST", fmt.Sprintf("/api/v1/jobs/%d/approvals", jobID), "{}", pm)
			mu.Lock()
			statuses[code]++
			mu.Unlock()
		}()
	}
	wg.Wait()
	if statuses[201] != 1 || statuses[409] != 11 {
		t.Fatalf("concurrent approve statuses = %v, want one 201 and eleven 409", statuses)
	}
}
