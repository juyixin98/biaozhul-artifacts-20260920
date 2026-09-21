package api_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"forensiccore/internal/domain"
	"forensiccore/internal/testkit"
)

func do(t *testing.T, env *testkit.Env, token, method, path string, body any) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	env.Engine.ServeHTTP(w, req)

	var parsed map[string]any
	if w.Body.Len() > 0 {
		_ = json.Unmarshal(w.Body.Bytes(), &parsed)
	}
	return w, parsed
}

// TestRolePermissions verifies the matrix:
//
//	investigator: register/transfer/note/query
//	analyst:       query/note only
//	anonymous:     rejected everywhere
func TestRolePermissions(t *testing.T) {
	env := testkit.NewEnv(t, 64*1024)
	defer env.Shutdown(t)
	inv := testkit.InvestigatorToken
	ana := testkit.AnalystToken

	// --- Case creation: investigator only ---
	w, body := do(t, env, inv, "POST", "/api/v1/cases", map[string]any{"name": "perm-case"})
	if w.Code != http.StatusCreated {
		t.Fatalf("investigator create case: code=%d body=%s", w.Code, w.Body.String())
	}
	caseID, _ := body["case"].(map[string]any)["id"].(string)

	// Analyst cannot create a case.
	if w, _ := do(t, env, ana, "POST", "/api/v1/cases", map[string]any{"name": "x"}); w.Code != http.StatusForbidden {
		t.Fatalf("analyst create case = %d, want 403", w.Code)
	}
	// No token at all -> 401.
	if w, _ := do(t, env, "", "POST", "/api/v1/cases", map[string]any{"name": "x"}); w.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous create case = %d, want 401", w.Code)
	}
	// Bad token -> 401.
	if w, _ := do(t, env, "nope", "POST", "/api/v1/cases", map[string]any{"name": "x"}); w.Code != http.StatusUnauthorized {
		t.Fatalf("bad token create case = %d, want 401", w.Code)
	}

	// --- Evidence registration: investigator only ---
	imagePath := env.WriteFile(t, "perm.dd", make([]byte, 2048))

	w, regBody := do(t, env, inv, "POST", "/api/v1/cases/"+caseID+"/evidence",
		map[string]any{"source_path": imagePath})
	if w.Code != http.StatusCreated {
		t.Fatalf("investigator register: code=%d body=%s", w.Code, w.Body.String())
	}
	evID, _ := regBody["evidence"].(map[string]any)["id"].(string)

	if w, _ := do(t, env, ana, "POST", "/api/v1/cases/"+caseID+"/evidence",
		map[string]any{"source_path": imagePath}); w.Code != http.StatusForbidden {
		t.Fatalf("analyst register = %d, want 403", w.Code)
	}

	// --- Queries: both roles allowed ---
	if w, _ := do(t, env, inv, "GET", "/api/v1/cases/"+caseID+"/evidence", nil); w.Code != http.StatusOK {
		t.Fatalf("investigator list evidence = %d", w.Code)
	}
	if w, _ := do(t, env, ana, "GET", "/api/v1/cases/"+caseID+"/evidence", nil); w.Code != http.StatusOK {
		t.Fatalf("analyst list evidence = %d, want 200", w.Code)
	}
	if w, _ := do(t, env, ana, "GET", "/api/v1/cases/"+caseID+"/verify", nil); w.Code != http.StatusOK {
		t.Fatalf("analyst verify = %d, want 200", w.Code)
	}

	// --- Notes: both roles allowed ---
	if w, _ := do(t, env, ana, "POST", "/api/v1/cases/"+caseID+"/notes",
		map[string]any{"evidence_id": evID, "note": "analyst remark"}); w.Code != http.StatusCreated {
		t.Fatalf("analyst note = %d body=%s, want 201", w.Code, w.Body.String())
	}

	// --- Transfer: investigator only ---
	w, _ = do(t, env, inv, "POST", "/api/v1/evidence/"+evID+"/transfer",
		map[string]any{"to_custodian": "lab-2", "reason": "analysis"})
	if w.Code != http.StatusCreated {
		t.Fatalf("investigator transfer = %d body=%s", w.Code, w.Body.String())
	}
	if w, b := do(t, env, ana, "POST", "/api/v1/evidence/"+evID+"/transfer",
		map[string]any{"to_custodian": "lab-3", "reason": "nope"}); w.Code != http.StatusForbidden {
		t.Fatalf("analyst transfer = %d body=%s, want 403", w.Code, b)
	}

	// --- Review start/resume: investigator only ---
	if w, _ := do(t, env, ana, "POST", "/api/v1/evidence/"+evID+"/reviews", nil); w.Code != http.StatusForbidden {
		t.Fatalf("analyst start review = %d, want 403", w.Code)
	}
	if w, _ := do(t, env, inv, "POST", "/api/v1/evidence/"+evID+"/reviews", nil); w.Code != http.StatusCreated {
		t.Fatalf("investigator start review = %d body=%s", w.Code, w.Body.String())
	}

	// --- Report: both roles; contains disclaimer ---
	w, repBody := do(t, env, ana, "GET", "/api/v1/cases/"+caseID+"/report", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("analyst report = %d", w.Code)
	}
	disclaimer, _ := repBody["disclaimer"].(string)
	if disclaimer == "" {
		t.Fatal("report must include the integrity-vs-trusted-time disclaimer")
	}
	verification, ok := repBody["verification"].(map[string]any)
	if !ok {
		t.Fatal("report missing verification")
	}
	if intact, _ := verification["intact"].(bool); !intact {
		t.Fatalf("report verification must be intact: %v", verification["issues"])
	}
	chainEvents, ok := repBody["chain"].([]any)
	if !ok || len(chainEvents) == 0 {
		t.Fatal("report must contain the full chain")
	}
	base, _ := repBody["evidence"].([]any)
	if len(base) != 1 {
		t.Fatalf("report should include baseline evidence rows, got %d", len(base))
	}
	reviews, _ := repBody["reviews"].([]any)
	if len(reviews) == 0 {
		t.Fatal("report should include review jobs")
	}
}

// TestRegisterPathEscapeOverHTTP surfaces path escape as 403.
func TestRegisterPathEscapeOverHTTP(t *testing.T) {
	env := testkit.NewEnv(t, 64*1024)
	defer env.Shutdown(t)
	w, _ := do(t, env, testkit.InvestigatorToken, "POST", "/api/v1/cases",
		map[string]any{"name": "escape-http"})
	caseID, _ := func() (string, error) {
		var b map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &b)
		id, _ := b["case"].(map[string]any)["id"].(string)
		return id, nil
	}()
	outside := env.WriteOutside(t, "escaped.dd", make([]byte, 64))
	w, _ = do(t, env, testkit.InvestigatorToken, "POST",
		"/api/v1/cases/"+caseID+"/evidence",
		map[string]any{"source_path": outside})
	if w.Code != http.StatusForbidden {
		t.Fatalf("outside registration = %d, want 403; body=%s", w.Code, w.Body.String())
	}
}

// TestReportAndChainEndToEnd performs a register/review/transfer/note flow and
// asserts verify reports an intact, gap-free chain over HTTP.
func TestReportAndChainEndToEnd(t *testing.T) {
	env := testkit.NewEnv(t, 4096)
	defer env.Shutdown(t)
	tok := testkit.InvestigatorToken

	w, b := do(t, env, tok, "POST", "/api/v1/cases", map[string]any{"name": "e2e"})
	caseID, _ := b["case"].(map[string]any)["id"].(string)

	path := env.WriteFile(t, "e2e.dd", make([]byte, 12*1024))
	w, b = do(t, env, tok, "POST", "/api/v1/cases/"+caseID+"/evidence",
		map[string]any{"source_path": path})
	if w.Code != 201 {
		t.Fatalf("register: %s", w.Body.String())
	}
	evID, _ := b["evidence"].(map[string]any)["id"].(string)

	// Start review and poll until completed.
	w, _ = do(t, env, tok, "POST", "/api/v1/evidence/"+evID+"/reviews", nil)
	if w.Code != 201 {
		t.Fatalf("start review: %s", w.Body.String())
	}
	completed := false
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		_, revBody := do(t, env, tok, "GET", "/api/v1/cases/"+caseID+"/reviews", nil)
		reviews, _ := revBody["reviews"].([]any)
		if len(reviews) == 1 && reviews[0].(map[string]any)["status"] == string(domain.ReviewCompleted) {
			completed = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !completed {
		t.Fatal("review did not complete in time")
	}

	w, _ = do(t, env, tok, "POST", "/api/v1/evidence/"+evID+"/transfer",
		map[string]any{"to_custodian": "lab-x", "reason": "ship"})
	if w.Code != 201 {
		t.Fatalf("transfer: %s", w.Body.String())
	}
	w, _ = do(t, env, testkit.AnalystToken, "POST", "/api/v1/cases/"+caseID+"/notes",
		map[string]any{"evidence_id": evID, "note": "sealed"})
	if w.Code != 201 {
		t.Fatalf("note: %s", w.Body.String())
	}

	_, v := do(t, env, tok, "GET", "/api/v1/cases/"+caseID+"/verify", nil)
	if intact, _ := v["intact"].(bool); !intact {
		t.Fatalf("verify failed: %v", v["issues"])
	}
	if head, _ := v["head_seq"].(float64); head < 5 {
		t.Fatalf("expected at least 5 chain events (case,register,review,transfer,note), got %v", head)
	}
}
