package integration

import (
	"context"
	"net/http"
	"sync"
	"testing"
)

// TestCloseGateRequiresPostmortemFields: cannot enter postmortem without root
// cause and lessons learned.
func TestPostmortemGateRequiresFields(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "gates")
	assignResponder(t, h, inc.ID)
	walkTo(t, h, inc.ID, 1, "recovered") // now at recovered, version 5

	// recovered -> postmortem without fields.
	w := transitionAs(t, h, uAnalyst, inc.ID, 5, "")
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 missing postmortem fields, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "gate_failed")

	// Only root cause -> still rejected.
	w = transitionAs(t, h, uAnalyst, inc.ID, 5, `"root_cause":"rc"`)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 missing lessons, got %d %s", w.Code, w.Body.String())
	}

	// Both -> accepted.
	w = transitionAs(t, h, uAnalyst, inc.ID, 5,
		`"root_cause":"rc","lessons_learned":"ll"`)
	if w.Code != http.StatusOK {
		t.Fatalf("postmortem with fields: %d %s", w.Code, w.Body.String())
	}
}

// TestCloseGateRequiresActionItem: closing fails when there is no action item.
func TestCloseGateRequiresActionItem(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "no items")
	assignResponder(t, h, inc.ID)
	walkTo(t, h, inc.ID, 1, "postmortem") // now at postmortem, version 6

	w := transitionAs(t, h, uResponder, inc.ID, 6,
		`"root_cause":"rc","lessons_learned":"ll"`)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 no action item, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "gate_failed")
}

// TestCloseGateRejectsOpenActionItem: an open action item blocks closing.
func TestCloseGateRejectsOpenActionItem(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "open item")
	assignResponder(t, h, inc.ID)
	walkTo(t, h, inc.ID, 1, "postmortem")

	// Create but leave it open.
	body := `{"title":"followup","owner_id":"` + uResponder + `","due_at":"2030-01-01T00:00:00Z"}`
	if w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/action-items",
		uAnalyst, reqID(), body); w.Code != http.StatusOK {
		t.Fatalf("create item: %d %s", w.Code, w.Body.String())
	}

	w := transitionAs(t, h, uResponder, inc.ID, 6,
		`"root_cause":"rc","lessons_learned":"ll"`)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 open item blocks close, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "gate_failed")
}

// TestEvidenceCapacitySequential: the 51st evidence item is rejected.
func TestEvidenceCapacitySequential(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "evidence cap")

	for i := 0; i < 50; i++ {
		w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
			uAnalyst, reqID(), `{"content":"e"}`)
		if w.Code != http.StatusOK {
			t.Fatalf("evidence %d: %d %s", i+1, w.Code, w.Body.String())
		}
	}
	w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
		uAnalyst, reqID(), `{"content":"over"}`)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("51st evidence expected 422, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "evidence_capacity")

	var cnt int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM evidence WHERE incident_id=$1`, inc.ID).Scan(&cnt); err != nil {
		t.Fatal(err)
	}
	if cnt != 50 {
		t.Fatalf("want exactly 50 evidence rows, got %d", cnt)
	}
}

// TestEvidenceCapacityConcurrent: racing adds cannot bypass the 50 cap.
func TestEvidenceCapacityConcurrent(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "race cap")

	// Pre-fill 48, then 5 concurrent inserts: exactly 2 must succeed.
	for i := 0; i < 48; i++ {
		if w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
			uAnalyst, reqID(), `{"content":"e"}`); w.Code != http.StatusOK {
			t.Fatalf("prefill %d: %d", i+1, w.Code)
		}
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	success := 0
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
				uAnalyst, reqID(), `{"content":"race"}`)
			mu.Lock()
			if w.Code == http.StatusOK {
				success++
			}
			mu.Unlock()
		}()
	}
	wg.Wait()
	if success != 2 {
		t.Fatalf("want exactly 2 concurrent inserts to succeed, got %d", success)
	}

	var cnt int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM evidence WHERE incident_id=$1`, inc.ID).Scan(&cnt); err != nil {
		t.Fatal(err)
	}
	if cnt != 50 {
		t.Fatalf("cap bypassed: want 50 rows, got %d", cnt)
	}
}

// TestEvidenceIsAppendOnly: corrections are notes; content is never overwritten.
func TestEvidenceIsAppendOnly(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "immutable")

	w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
		uAnalyst, reqID(), `{"content":"original observation"}`)
	if w.Code != http.StatusOK {
		t.Fatalf("add evidence: %d %s", w.Code, w.Body.String())
	}
	var ev struct {
		ID      string `json:"id"`
		Content string `json:"content"`
	}
	decodeBody(t, w, &ev)

	// There is no overwrite endpoint; a PUT is not routed.
	wPut := doJSON(t, h, http.MethodPut,
		"/v1/incidents/"+inc.ID+"/evidence/"+ev.ID,
		uAnalyst, reqID(), `{"content":"tampered"}`)
	if wPut.Code != http.StatusMethodNotAllowed && wPut.Code != http.StatusNotFound {
		t.Fatalf("overwrite endpoint must not exist, got %d", wPut.Code)
	}

	// A correction is appended as a linked note.
	w = doJSON(t, h, http.MethodPost,
		"/v1/incidents/"+inc.ID+"/evidence/"+ev.ID+"/notes",
		uAnalyst, reqID(), `{"content":"correction: timestamp was UTC+2"}`)
	if w.Code != http.StatusOK {
		t.Fatalf("append note: %d %s", w.Code, w.Body.String())
	}
	var note struct {
		ID string `json:"id"`
	}
	decodeBody(t, w, &note)
	if note.ID == "" {
		t.Fatal("note id missing")
	}

	// Original content is unchanged and the export carries both evidence and
	// its appended correction.
	w = doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID+"/export",
		uAnalyst, "", "")
	if w.Code != http.StatusOK {
		t.Fatalf("export: %d %s", w.Code, w.Body.String())
	}
	var export struct {
		Evidence []struct {
			ID      string `json:"id"`
			Content string `json:"content"`
			Notes   []struct {
				Content string `json:"content"`
			} `json:"notes"`
		} `json:"evidence_summary"`
	}
	decodeBody(t, w, &export)
	if len(export.Evidence) != 1 {
		t.Fatalf("want 1 evidence in export, got %d", len(export.Evidence))
	}
	if export.Evidence[0].Content != "original observation" {
		t.Fatalf("evidence content was modified: %q", export.Evidence[0].Content)
	}
	if len(export.Evidence[0].Notes) != 1 ||
		export.Evidence[0].Notes[0].Content != "correction: timestamp was UTC+2" {
		t.Fatalf("correction note missing in export: %+v", export.Evidence[0].Notes)
	}
}
