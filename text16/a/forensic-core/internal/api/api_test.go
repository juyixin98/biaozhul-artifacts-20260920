package api_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"forensiccore/internal/api"
	"forensiccore/internal/chain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/safeopen"
	"forensiccore/internal/testutil"
)

type fixture struct {
	router *gin.Engine
	db     *gorm.DB
	root   string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	gin.SetMode(gin.TestMode)
	db := testutil.NewDB(t)
	rootDir := t.TempDir()
	// 一个小镜像样例。
	img := make([]byte, 4096)
	for i := range img {
		img[i] = byte(i % 253)
	}
	if err := os.WriteFile(filepath.Join(rootDir, "sample.dd"), img, 0o644); err != nil {
		t.Fatal(err)
	}
	root, err := safeopen.NewRoot(rootDir)
	if err != nil {
		t.Fatal(err)
	}
	chainStore := chain.NewStore(db)
	svc := evidence.NewService(db, root, chainStore, 1024)
	return &fixture{router: api.NewRouter(db, svc, chainStore), db: db, root: rootDir}
}

func (f *fixture) do(t *testing.T, method, path, user, role string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if user != "" {
		req.Header.Set("X-User-Name", user)
	}
	if role != "" {
		req.Header.Set("X-User-Role", role)
	}
	w := httptest.NewRecorder()
	f.router.ServeHTTP(w, req)
	return w
}

func decode(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
		t.Fatalf("decode %q: %v", w.Body.String(), err)
	}
	return m
}

func (f *fixture) createCase(t *testing.T) uint {
	t.Helper()
	w := f.do(t, "POST", "/api/cases", "inv1", "investigator",
		map[string]any{"name": "case-" + t.Name(), "description": "test"})
	if w.Code != http.StatusCreated {
		t.Fatalf("create case: %d %s", w.Code, w.Body.String())
	}
	return uint(decode(t, w)["id"].(float64))
}

func TestAuthRequired(t *testing.T) {
	fx := newFixture(t)
	w := fx.do(t, "GET", "/api/cases", "", "", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("no headers: %d, want 401", w.Code)
	}
	w = fx.do(t, "GET", "/api/cases", "x", "superadmin", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("bad role: %d, want 401", w.Code)
	}
}

// 权限限制：分析师只能查询和备注；登记、复核、移交仅调查员。
func TestRolePermissions(t *testing.T) {
	fx := newFixture(t)
	caseID := fx.createCase(t)
	base := fmt.Sprintf("/api/cases/%d", caseID)

	// 分析师不能建案件。
	w := fx.do(t, "POST", "/api/cases", "ana1", "analyst", map[string]any{"name": "x"})
	if w.Code != http.StatusForbidden {
		t.Fatalf("analyst create case: %d, want 403", w.Code)
	}
	// 分析师不能登记证据。
	w = fx.do(t, "POST", base+"/evidence", "ana1", "analyst", map[string]any{"path": "sample.dd"})
	if w.Code != http.StatusForbidden {
		t.Fatalf("analyst register: %d, want 403", w.Code)
	}
	// 调查员登记证据。
	w = fx.do(t, "POST", base+"/evidence", "inv1", "investigator", map[string]any{"path": "sample.dd"})
	if w.Code != http.StatusCreated {
		t.Fatalf("investigator register: %d %s", w.Code, w.Body.String())
	}
	evID := uint(decode(t, w)["id"].(float64))

	// 分析师不能发起复核、不能移交。
	w = fx.do(t, "POST", fmt.Sprintf("/api/evidence/%d/verify-jobs", evID), "ana1", "analyst", nil)
	if w.Code != http.StatusForbidden {
		t.Fatalf("analyst verify job: %d, want 403", w.Code)
	}
	w = fx.do(t, "POST", base+"/transfers", "ana1", "analyst", map[string]any{"to": "lab"})
	if w.Code != http.StatusForbidden {
		t.Fatalf("analyst transfer: %d, want 403", w.Code)
	}
	// 分析师可以备注、查询。
	w = fx.do(t, "POST", base+"/notes", "ana1", "analyst", map[string]any{"text": "初步检查无异常"})
	if w.Code != http.StatusCreated {
		t.Fatalf("analyst note: %d %s", w.Code, w.Body.String())
	}
	for _, p := range []string{base + "/evidence", base + "/chain", base + "/chain/verify", base + "/report"} {
		if w := fx.do(t, "GET", p, "ana1", "analyst", nil); w.Code != http.StatusOK {
			t.Fatalf("analyst GET %s: %d", p, w.Code)
		}
	}
	// 调查员复核与移交。
	w = fx.do(t, "POST", fmt.Sprintf("/api/evidence/%d/verify-jobs", evID), "inv1", "investigator", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("verify job: %d %s", w.Code, w.Body.String())
	}
	if got := decode(t, w)["result"]; got != "match" {
		t.Fatalf("verify result = %v", got)
	}
	w = fx.do(t, "POST", base+"/transfers", "inv1", "investigator", map[string]any{"to": "证物室B"})
	if w.Code != http.StatusCreated {
		t.Fatalf("transfer: %d %s", w.Code, w.Body.String())
	}
}

// 报告包含基线、复核结果与完整证据链，链校验通过。
func TestReportAndChainVerify(t *testing.T) {
	fx := newFixture(t)
	caseID := fx.createCase(t)
	base := fmt.Sprintf("/api/cases/%d", caseID)

	w := fx.do(t, "POST", base+"/evidence", "inv1", "investigator", map[string]any{"path": "sample.dd"})
	evID := uint(decode(t, w)["id"].(float64))
	fx.do(t, "POST", fmt.Sprintf("/api/evidence/%d/verify-jobs", evID), "inv1", "investigator", nil)
	fx.do(t, "POST", base+"/notes", "ana1", "analyst", map[string]any{"text": "note"})
	fx.do(t, "POST", base+"/transfers", "inv1", "investigator", map[string]any{"to": "lab"})

	w = fx.do(t, "GET", base+"/chain/verify", "ana1", "analyst", nil)
	if ok := decode(t, w)["ok"]; ok != true {
		t.Fatalf("chain verify failed: %s", w.Body.String())
	}

	w = fx.do(t, "GET", base+"/report", "inv1", "investigator", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("report: %d", w.Code)
	}
	rep := decode(t, w)
	if len(rep["baseline"].([]any)) != 1 {
		t.Fatalf("baseline entries: %v", rep["baseline"])
	}
	if len(rep["verify_jobs"].([]any)) != 1 {
		t.Fatalf("verify jobs: %v", rep["verify_jobs"])
	}
	chainEvents := rep["chain"].([]any)
	if len(chainEvents) != 4 { // register, verify, note, transfer
		t.Fatalf("chain events = %d, want 4", len(chainEvents))
	}
	if rep["disclaimer"] == nil || rep["disclaimer"] == "" {
		t.Fatal("report missing disclaimer")
	}
}

// 登记路径越界返回错误且不产生记录。
func TestRegisterPathTraversalRejected(t *testing.T) {
	fx := newFixture(t)
	caseID := fx.createCase(t)
	w := fx.do(t, "POST", fmt.Sprintf("/api/cases/%d/evidence", caseID),
		"inv1", "investigator", map[string]any{"path": "../../etc/passwd"})
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("traversal: %d, want 422", w.Code)
	}
	w = fx.do(t, "GET", fmt.Sprintf("/api/cases/%d/evidence", caseID), "inv1", "investigator", nil)
	if got := len(decodeList(t, w)); got != 0 {
		t.Fatalf("evidence rows = %d, want 0", got)
	}
}

func decodeList(t *testing.T, w *httptest.ResponseRecorder) []any {
	t.Helper()
	var l []any
	if err := json.Unmarshal(w.Body.Bytes(), &l); err != nil {
		t.Fatalf("decode %q: %v", w.Body.String(), err)
	}
	return l
}
