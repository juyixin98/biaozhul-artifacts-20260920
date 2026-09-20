package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"

	"proofcycle/internal/database"
	"proofcycle/internal/httpapi"
	"proofcycle/internal/model"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

const defaultTestDSN = "proofcycle:proofcycle_pw@tcp(127.0.0.1:3306)/proofcycle_test?charset=utf8mb4&parseTime=true&loc=UTC"

// truncateOrder covers every business table; schema_migrations is retained.
var truncateOrder = []string{
	"signoffs", "opinions", "checklist_items", "review_rounds",
	"file_versions", "job_checklist_items", "job_reviewers", "jobs", "users",
}

type app struct {
	t       *testing.T
	db      *gorm.DB
	store   *storage.Storage
	svc     *service.Service
	server  *httptest.Server
	rootDir string
}

func newApp(t *testing.T, maxUploadMB int64) *app {
	t.Helper()
	dsn := os.Getenv("PROOFCYCLE_TEST_DSN")
	if dsn == "" {
		dsn = defaultTestDSN
	}
	db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{})
	if err != nil {
		if os.Getenv("PROOFCYCLE_TEST_DSN") != "" {
			t.Fatalf("connect test database: %v", err)
		}
		t.Skipf("test database unavailable, skipping: %v", err)
	}
	if err = database.Migrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	cleanDB(t, db)

	dir := t.TempDir()
	store, err := storage.New(filepath.Join(dir, "storage"))
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	svc := service.New(db, store, maxUploadMB*1024*1024)
	engine := httpapi.NewEngine(svc, "bootstrap-secret")
	srv := httptest.NewServer(engine)
	a := &app{t: t, db: db, store: store, svc: svc, server: srv, rootDir: dir}
	t.Cleanup(func() {
		srv.Close()
		cleanDB(t, db)
	})
	return a
}

func cleanDB(t *testing.T, db *gorm.DB) {
	t.Helper()
	if err := db.Exec("SET FOREIGN_KEY_CHECKS = 0").Error; err != nil {
		t.Fatalf("disable fk checks: %v", err)
	}
	for _, tbl := range truncateOrder {
		if err := db.Exec("TRUNCATE TABLE " + tbl).Error; err != nil {
			t.Fatalf("truncate %s: %v", tbl, err)
		}
	}
	if err := db.Exec("SET FOREIGN_KEY_CHECKS = 1").Error; err != nil {
		t.Fatalf("enable fk checks: %v", err)
	}
}

// ---- user / job fixtures ----

func (a *app) mustUser(role string) *model.User {
	a.t.Helper()
	name := fmt.Sprintf("%s-%d-%p", role, len(role), a)
	u, err := a.svc.CreateUser(context.Background(), service.CreateUserInput{
		Name: strings.ReplaceAll(name, "0x", "u"), Role: role,
	})
	if err != nil {
		a.t.Fatalf("create user %s: %v", role, err)
	}
	return u
}

type jobFixture struct {
	id        int64
	designer  *model.User
	pm        *model.User
	reviewers []*model.User
	checklist []string
}

func (a *app) mustJob(designer, pm *model.User, reviewers []*model.User, checklist []string) *jobFixture {
	a.t.Helper()
	ids := make([]int64, len(reviewers))
	for i, r := range reviewers {
		ids[i] = r.ID
	}
	items := make([]service.ChecklistInput, len(checklist))
	for i, code := range checklist {
		items[i] = service.ChecklistInput{Code: code, Description: "check " + code}
	}
	job, err := a.svc.CreateJob(context.Background(), designer, service.CreateJobInput{
		Name: fmt.Sprintf("job-%p-%s", a, checklist[0]), PMID: pm.ID,
		ReviewerIDs: ids, Checklist: items,
	})
	if err != nil {
		a.t.Fatalf("create job: %v", err)
	}
	return &jobFixture{
		id: job.ID, designer: designer, pm: pm, reviewers: reviewers, checklist: checklist,
	}
}

// ---- HTTP helpers ----

func (a *app) do(method, path, token string, body any) (int, map[string]any) {
	a.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, a.server.URL+path, rdr)
	if err != nil {
		a.t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("X-API-Token", token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func (a *app) upload(token string, jobID int64, fileName string, content []byte) (int, map[string]any) {
	a.t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, err := mw.CreateFormFile("file", fileName)
	if err != nil {
		a.t.Fatal(err)
	}
	if _, err = fw.Write(content); err != nil {
		a.t.Fatal(err)
	}
	mw.Close()
	req, err := http.NewRequest(http.MethodPost,
		fmt.Sprintf("%s/api/v1/jobs/%d/revisions", a.server.URL, jobID), &buf)
	if err != nil {
		a.t.Fatal(err)
	}
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("X-API-Token", token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

func (a *app) download(token string, jobID int64, version int) (int, []byte, http.Header) {
	a.t.Helper()
	req, _ := http.NewRequest(http.MethodGet,
		fmt.Sprintf("%s/api/v1/jobs/%d/versions/%d/file", a.server.URL, jobID, version), nil)
	req.Header.Set("X-API-Token", token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, raw, resp.Header
}

// submitAll records the same verdict for every checklist code on behalf of a
// reviewer.
func (a *app) submitAll(token string, jobID int64, version int, reviewer *model.User, verdict, reason string, expect int) (int, map[string]any) {
	a.t.Helper()
	detail := a.jobDetail(token, jobID)
	round := findRound(detail, version)
	items := make([]service.OpinionItemInput, 0, len(round["checklist"].([]any)))
	for _, ci := range round["checklist"].([]any) {
		m := ci.(map[string]any)
		items = append(items, service.OpinionItemInput{
			ChecklistCode: m["code"].(string), Verdict: verdict, Reason: reason, ExpectedVersion: expect,
		})
	}
	return a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID), token,
		service.SubmitOpinionsInput{Version: version, Items: items})
}

func (a *app) jobDetail(token string, jobID int64) map[string]any {
	a.t.Helper()
	status, body := a.do(http.MethodGet, fmt.Sprintf("/api/v1/jobs/%d", jobID), token, nil)
	if status != http.StatusOK {
		a.t.Fatalf("job detail status=%d body=%v", status, body)
	}
	return body
}

func findRound(detail map[string]any, version int) map[string]any {
	for _, r := range detail["rounds"].([]any) {
		m := r.(map[string]any)
		if int(m["version"].(float64)) == version {
			return m
		}
	}
	return nil
}

func (a *app) signoff(token string, jobID int64) (int, map[string]any) {
	return a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/signoff", jobID), token, nil)
}

// ---- fixtures ----

func pdfBytes() []byte {
	return []byte("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
}

func pngBytes() []byte {
	return []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D}
}

func paddedPDF(size int) []byte {
	b := pdfBytes()
	return append(b, make([]byte, size-len(b))...)
}
