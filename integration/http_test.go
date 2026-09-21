package integration

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"

	"proofcycle/internal/domain"
	"proofcycle/internal/httpapi"
	"proofcycle/internal/seed"
)

func newServer(e *testEnv) http.Handler {
	return httpapi.New(e.svc, 1<<20).Handler()
}

func doJSON(t *testing.T, h http.Handler, method, path, user string, body []byte) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if user != "" {
		req.Header.Set("X-User-Id", user)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	return w
}

func TestHTTPRequiresAuth(t *testing.T) {
	e := newTestEnv(t)
	h := newServer(e)
	w := doJSON(t, h, http.MethodGet, "/api/v1/jobs", "", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("no auth: code=%d want 401", w.Code)
	}
	w = doJSON(t, h, http.MethodGet, "/api/v1/jobs", "no-such-user", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("bad user: code=%d want 401", w.Code)
	}
}

func TestHTTPNonMemberGets404(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})
	h := newServer(e)

	for _, p := range []string{
		"/api/v1/jobs/" + job.ID,
		"/api/v1/jobs/" + job.ID + "/versions",
		"/api/v1/jobs/" + job.ID + "/versions/1/report",
		"/api/v1/jobs/" + job.ID + "/versions/1/history",
	} {
		w := doJSON(t, h, http.MethodGet, p, seed.Reviewer2, nil)
		if w.Code != http.StatusNotFound {
			t.Fatalf("non-member GET %s: code=%d want 404", p, w.Code)
		}
	}
}

func TestHTTPCreateJobOnlyPMAsSelf(t *testing.T) {
	e := newTestEnv(t)
	h := newServer(e)
	body := []byte(`{"name":"http job","designer_id":"` + seed.UserDesigner + `","pm_id":"` + seed.UserPM + `",
		"reviewer_ids":["` + seed.Reviewer1 + `"],"checklist_id":"` + seed.DefaultChecklist + `"}`)

	// 审查员不能创建作业。
	w := doJSON(t, h, http.MethodPost, "/api/v1/jobs", seed.Reviewer1, body)
	if w.Code != http.StatusForbidden {
		t.Fatalf("reviewer create: code=%d want 403", w.Code)
	}
	// PM 必须以自己为 PM（这里就是自己）-> 成功。
	w = doJSON(t, h, http.MethodPost, "/api/v1/jobs", seed.UserPM, body)
	if w.Code != http.StatusCreated {
		t.Fatalf("pm create: code=%d body=%s", w.Code, w.Body.String())
	}
}

func TestHTTPMultipartUploadAndDownload(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})
	h := newServer(e)

	pdf := validPDF()
	sum := sha256.Sum256(pdf)
	wantSHA := hex.EncodeToString(sum[:])

	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	_ = mw.WriteField("sha256", wantSHA)
	part, err := mw.CreateFormFile("file", "proof.pdf")
	if err != nil {
		t.Fatal(err)
	}
	part.Write(pdf)
	mw.Close()

	req := httptest.NewRequest(http.MethodPost, "/api/v1/jobs/"+job.ID+"/versions", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("X-User-Id", seed.UserDesigner)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusCreated {
		t.Fatalf("upload: code=%d body=%s", w.Code, w.Body.String())
	}

	// 下载并比对 SHA-256。
	dl := doJSON(t, h, http.MethodGet, "/api/v1/jobs/"+job.ID+"/versions/1/download", seed.Reviewer1, nil)
	if dl.Code != http.StatusOK {
		t.Fatalf("download: code=%d body=%s", dl.Code, dl.Body.String())
	}
	if dl.Header().Get("X-Content-SHA256") != wantSHA {
		t.Fatalf("download sha header wrong: %s", dl.Header().Get("X-Content-SHA256"))
	}
	got := sha256.Sum256(dl.Body.Bytes())
	if hex.EncodeToString(got[:]) != wantSHA {
		t.Fatalf("downloaded content mismatch")
	}
	// 非成员不能下载。
	dl2 := doJSON(t, h, http.MethodGet, "/api/v1/jobs/"+job.ID+"/versions/1/download", seed.Reviewer2, nil)
	if dl2.Code != http.StatusNotFound {
		t.Fatalf("outsider download: code=%d want 404", dl2.Code)
	}
}

func TestHTTPReviewerCannotApprove(t *testing.T) {
	e, job, ids := twoReviewerEnvPublic(t)
	h := newServer(e)
	for _, r := range []string{seed.Reviewer1, seed.Reviewer2} {
		items := allPassInputs(ids)
		body, _ := encodeReviewSubmit(1, items)
		req := httptest.NewRequest(http.MethodPost, "/api/v1/jobs/"+job.ID+"/reviews", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-User-Id", r)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != http.StatusCreated {
			t.Fatalf("submit review %s: code=%d %s", r, w.Code, w.Body.String())
		}
	}
	// 审查员尝试调签核接口 -> 403。
	w := doJSON(t, h, http.MethodPost, "/api/v1/jobs/"+job.ID+"/approvals", seed.Reviewer1, []byte(`{}`))
	if w.Code != http.StatusForbidden {
		t.Fatalf("reviewer approve: code=%d want 403 body=%s", w.Code, w.Body.String())
	}
	// PM 成功签核。
	w = doJSON(t, h, http.MethodPost, "/api/v1/jobs/"+job.ID+"/approvals", seed.UserPM, []byte(`{}`))
	if w.Code != http.StatusCreated {
		t.Fatalf("pm approve: code=%d body=%s", w.Code, w.Body.String())
	}
}

// 防止未使用导入报错（domain 在该文件仅间接使用）。
var _ = domain.StatusApproved
var _ = io.EOF
