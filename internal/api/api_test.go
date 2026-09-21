package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"forensiccore/internal/models"
	"forensiccore/internal/testutil"
)

type client struct {
	base  string
	token string
	h     *http.Client
}

func (cl *client) do(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, cl.base+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if cl.token != "" {
		req.Header.Set("Authorization", "Bearer "+cl.token)
	}
	resp, err := cl.h.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func setup(t *testing.T, chunkSize int) (*testutil.Env, *client, *client) {
	t.Helper()
	env := testutil.New(t, chunkSize, nil)
	srv := env.HTTPServer(t)
	t.Cleanup(srv.Close)
	inv := &client{base: srv.URL, token: env.InvToken, h: srv.Client()}
	an := &client{base: srv.URL, token: env.AnToken, h: srv.Client()}
	return env, inv, an
}

func registerViaAPI(t *testing.T, inv *client, ref, file string) (int, map[string]any) {
	return inv.do(t, http.MethodPost, "/api/v1/cases", map[string]any{"case_ref": ref, "file": file})
}

func writeEv(t *testing.T, env *testutil.Env, name string, data []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(env.EvidenceRoot, name), data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestAuthRequired(t *testing.T) {
	env, _, _ := setup(t, 16)
	srv := env.HTTPServer(t)
	t.Cleanup(srv.Close)
	none := &client{base: srv.URL, token: "", h: srv.Client()}
	code, body := none.do(t, http.MethodGet, "/api/v1/cases", nil)
	if code != http.StatusUnauthorized || body == nil {
		t.Fatalf("unauthenticated request: code=%d body=%v", code, body)
	}
	bad := &client{base: srv.URL, token: "wrong", h: srv.Client()}
	code, _ = bad.do(t, http.MethodGet, "/api/v1/cases", nil)
	if code != http.StatusUnauthorized {
		t.Fatalf("bad token: code=%d", code)
	}
}

func TestRoleRestrictions(t *testing.T) {
	env, inv, an := setup(t, 16)
	writeEv(t, env, "disk.raw", []byte("role-test-image-content"))

	// Analyst cannot register.
	code, _ := an.do(t, http.MethodPost, "/api/v1/cases", map[string]any{"case_ref": "R1", "file": "disk.raw"})
	if code != http.StatusForbidden {
		t.Fatalf("analyst register: code=%d, want 403", code)
	}

	code, body := registerViaAPI(t, inv, "R1", "disk.raw")
	if code != http.StatusCreated {
		t.Fatalf("investigator register: code=%d body=%v", code, body)
	}
	id := int(body["id"].(float64))

	// Analyst cannot transfer or start verification.
	code, _ = an.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/transfers", id),
		map[string]any{"from": "desk", "to": "lab"})
	if code != http.StatusForbidden {
		t.Fatalf("analyst transfer: code=%d, want 403", code)
	}
	code, _ = an.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/verifications", id), nil)
	if code != http.StatusForbidden {
		t.Fatalf("analyst verify: code=%d, want 403", code)
	}

	// Investigator CAN transfer and verify.
	code, _ = inv.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/transfers", id),
		map[string]any{"from": "desk", "to": "lab", "reason": "analysis"})
	if code != http.StatusCreated {
		t.Fatalf("investigator transfer: code=%d", code)
	}
	code, _ = inv.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/verifications", id),
		map[string]any{"chunk_size": 16})
	if code != http.StatusCreated {
		t.Fatalf("investigator verify: code=%d", code)
	}

	// Analyst CAN query and add a note.
	code, body = an.do(t, http.MethodGet, fmt.Sprintf("/api/v1/cases/%d", id), nil)
	if code != http.StatusOK {
		t.Fatalf("analyst get: code=%d", code)
	}
	if body["case_ref"] != "R1" {
		t.Fatalf("unexpected body: %v", body)
	}
	code, body = an.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/notes", id),
		map[string]any{"body": "analyst observation"})
	if code != http.StatusCreated {
		t.Fatalf("analyst note: code=%d body=%v", code, body)
	}
	if body["actor"] != "bob" {
		t.Fatalf("note actor = %v", body["actor"])
	}
}

func TestRegistrationRejectsTraversalViaAPI(t *testing.T) {
	env, inv, _ := setup(t, 16)
	outside := filepath.Join(t.TempDir(), "secret.raw")
	if err := os.WriteFile(outside, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	_ = env
	code, body := registerViaAPI(t, inv, "TRAV", "../../secret.raw")
	if code != http.StatusBadRequest {
		t.Fatalf("traversal code=%d body=%v", code, body)
	}

	// Symlink escape is also rejected.
	link := filepath.Join(env.EvidenceRoot, "link.raw")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	code, _ = registerViaAPI(t, inv, "TRAV2", "link.raw")
	if code != http.StatusBadRequest {
		t.Fatalf("symlink escape code=%d, want 400", code)
	}
}

func TestConcurrentNoteAppendsViaAPI(t *testing.T) {
	env, inv, an := setup(t, 16)
	writeEv(t, env, "chain.raw", bytes.Repeat([]byte{0xAB}, 128))
	code, body := registerViaAPI(t, inv, "CONC", "chain.raw")
	if code != http.StatusCreated {
		t.Fatalf("register: %d %v", code, body)
	}
	id := int(body["id"].(float64))

	const writers = 12
	const perWriter = 5
	var wg sync.WaitGroup
	statuses := make(chan int, writers*perWriter)
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			cl := inv
			if w%2 == 0 {
				cl = an // analysts may note too
			}
			for i := 0; i < perWriter; i++ {
				code, _ := cl.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/notes", id),
					map[string]any{"body": fmt.Sprintf("note w%d n%d", w, i)})
				statuses <- code
			}
		}(w)
	}
	wg.Wait()
	close(statuses)
	for s := range statuses {
		if s != http.StatusCreated {
			t.Fatalf("concurrent append status = %d, want 201", s)
		}
	}

	code, vbody := inv.do(t, http.MethodGet, fmt.Sprintf("/api/v1/cases/%d/verify", id), nil)
	if code != http.StatusOK {
		t.Fatalf("verify code=%d", code)
	}
	if vbody["ok"] != true {
		t.Fatalf("chain verification after concurrent appends failed: %v", vbody["faults"])
	}
	events := vbody
	_ = events
	code, ebody := inv.do(t, http.MethodGet, fmt.Sprintf("/api/v1/cases/%d/events", id), nil)
	if code != http.StatusOK {
		t.Fatalf("events code=%d", code)
	}
	list := ebody["events"].([]any)
	// 1 register + writers*perWriter notes
	if len(list) != 1+writers*perWriter {
		t.Fatalf("event count = %d, want %d", len(list), 1+writers*perWriter)
	}
}

func TestVerifyEndpointReportsTampering(t *testing.T) {
	env, inv, _ := setup(t, 16)
	writeEv(t, env, "t.raw", []byte("tamper-check-image"))
	_, body := registerViaAPI(t, inv, "TAMP", "t.raw")
	id := int(body["id"].(float64))

	// Tamper directly at the persistence layer (simulates a modified row).
	if err := env.DB.Exec(`UPDATE chain_events SET actor = 'mallory' WHERE case_id = ? AND seq = 1`, id).Error; err != nil {
		t.Fatal(err)
	}
	code, vbody := inv.do(t, http.MethodGet, fmt.Sprintf("/api/v1/cases/%d/verify", id), nil)
	if code != http.StatusOK {
		t.Fatalf("verify code=%d", code)
	}
	if vbody["ok"] != false {
		t.Fatalf("expected ok=false, got %v", vbody)
	}
	faults := vbody["faults"].([]any)
	if len(faults) == 0 {
		t.Fatal("expected at least one fault")
	}
	f0 := faults[0].(map[string]any)
	if f0["code"] != "TAMPERED" {
		t.Fatalf("fault code = %v", f0["code"])
	}
}

func TestExportReportContents(t *testing.T) {
	env, inv, an := setup(t, 16)
	data := []byte("export report fixture contents here")
	writeEv(t, env, "export.raw", data)
	_, body := registerViaAPI(t, inv, "EXPORT", "export.raw")
	id := int(body["id"].(float64))

	// Add a transfer, a verification and a note before exporting.
	if code, _ := inv.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/transfers", id),
		map[string]any{"to": "lab-7"}); code != 201 {
		t.Fatalf("transfer %d", code)
	}
	env.Runner.Start(context.Background())
	defer env.Runner.Stop()
	inv.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/verifications", id),
		map[string]any{"chunk_size": 16})
	// Wait for completion.
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		var j models.VerificationJob
		if err := env.DB.First(&j).Error; err == nil &&
			(j.Status == models.JobCompletedMatch) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	an.do(t, http.MethodPost, fmt.Sprintf("/api/v1/cases/%d/notes", id), map[string]any{"body": "final note"})

	code, rep := inv.do(t, http.MethodGet, fmt.Sprintf("/api/v1/cases/%d/export", id), nil)
	if code != http.StatusOK {
		t.Fatalf("export code=%d", code)
	}
	if rep["report_version"] == nil {
		t.Fatal("missing report_version")
	}
	baseline := rep["baseline"].(map[string]any)
	if baseline["case_ref"] != "EXPORT" {
		t.Fatalf("baseline: %v", baseline)
	}
	if baseline["sha256"] == nil || baseline["size"].(float64) != float64(len(data)) {
		t.Fatalf("baseline missing digest/size: %v", baseline)
	}
	verifs := rep["verifications"].([]any)
	if len(verifs) != 1 {
		t.Fatalf("verifications = %d", len(verifs))
	}
	chainView := rep["chain"].(map[string]any)
	if chainView["ok"] != true {
		t.Fatalf("export chain not ok: %v", chainView)
	}
	events := chainView["events"].([]any)
	if len(events) != 4 { // register, transfer, verification, note
		t.Fatalf("export events = %d, want 4", len(events))
	}
	lim, _ := rep["limitations"].(string)
	if lim == "" || !bytes.Contains([]byte(lim), []byte("trusted timestamp")) {
		t.Fatalf("export must explain hash-chain limitations, got %q", lim)
	}
}

func TestHealthz(t *testing.T) {
	env, _, _ := setup(t, 16)
	srv := env.HTTPServer(t)
	t.Cleanup(srv.Close)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz = %d", resp.StatusCode)
	}
}
