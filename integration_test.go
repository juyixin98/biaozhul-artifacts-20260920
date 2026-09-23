package tpc

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func startCoordinator(t *testing.T, walPath string) (*Coordinator, *httptest.Server) {
	t.Helper()
	c, err := NewCoordinator(walPath, 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(c.Handler())
	t.Cleanup(func() { srv.Close(); c.Close() })
	return c, srv
}

func startParticipant(t *testing.T, walPath, coordURL string) (*Participant, *httptest.Server) {
	t.Helper()
	p, err := NewParticipant(walPath, coordURL, 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(p.Handler())
	t.Cleanup(func() { srv.Close(); p.Close() })
	return p, srv
}

func post(t *testing.T, url, body string) (int, string) {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

func getJSON(t *testing.T, url string) map[string]any {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	var v map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		t.Fatalf("GET %s: decode: %v", url, err)
	}
	return v
}

// txState returns the participant's state for txID ("" if unknown).
func txState(t *testing.T, participantURL, txID string) string {
	t.Helper()
	v := getJSON(t, participantURL+"/status")
	txs, _ := v["txs"].(map[string]any)
	if tx, ok := txs[txID].(map[string]any); ok {
		s, _ := tx["state"].(string)
		return s
	}
	return ""
}

func waitFor(t *testing.T, what string, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestCommitHappyPath(t *testing.T) {
	dir := t.TempDir()
	_, coord := startCoordinator(t, filepath.Join(dir, "c.wal"))
	_, pa := startParticipant(t, filepath.Join(dir, "a.wal"), coord.URL)
	_, pb := startParticipant(t, filepath.Join(dir, "b.wal"), coord.URL)

	body := fmt.Sprintf(`{"id":"tx1","participants":[%q,%q],"payload":{"resource":"r1"}}`, pa.URL, pb.URL)
	if code, resp := post(t, coord.URL+"/tx", body); code != http.StatusCreated {
		t.Fatalf("create: %d %s", code, resp)
	}
	code, resp := post(t, coord.URL+"/tx/tx1/commit", "")
	if code != http.StatusOK || !strings.Contains(resp, StateCommit) {
		t.Fatalf("commit: %d %s", code, resp)
	}
	if s := txState(t, pa.URL, "tx1"); s != StateCommitted {
		t.Fatalf("participant A: want COMMITTED, got %s", s)
	}
	if s := txState(t, pb.URL, "tx1"); s != StateCommitted {
		t.Fatalf("participant B: want COMMITTED, got %s", s)
	}
	v := getJSON(t, coord.URL+"/tx/tx1")
	if v["state"] != StateDone || v["decision"] != StateCommit {
		t.Fatalf("coordinator: want DONE/COMMIT, got %v", v)
	}
}

func TestVoteNoLeadsToAbort(t *testing.T) {
	dir := t.TempDir()
	_, coord := startCoordinator(t, filepath.Join(dir, "c.wal"))
	_, pa := startParticipant(t, filepath.Join(dir, "a.wal"), coord.URL)
	_, pb := startParticipant(t, filepath.Join(dir, "b.wal"), coord.URL)

	body := fmt.Sprintf(`{"id":"tx1","participants":[%q,%q],"payload":{"vote":"no"}}`, pa.URL, pb.URL)
	post(t, coord.URL+"/tx", body)
	code, resp := post(t, coord.URL+"/tx/tx1/commit", "")
	if code != http.StatusOK || !strings.Contains(resp, StateAbort) {
		t.Fatalf("commit: %d %s", code, resp)
	}
	for _, p := range []string{pa.URL, pb.URL} {
		if s := txState(t, p, "tx1"); s != StateAborted {
			t.Fatalf("participant %s: want ABORTED, got %s", p, s)
		}
	}
}

// A prepared participant holds its resource lock; a conflicting prepare is
// rejected, which forces the coordinator to abort that transaction.
func TestResourceLockBlocking(t *testing.T) {
	dir := t.TempDir()
	_, pa := startParticipant(t, filepath.Join(dir, "a.wal"), "")

	if code, _ := post(t, pa.URL+"/prepare", `{"txId":"tx1","payload":{"resource":"r1"}}`); code != http.StatusOK {
		t.Fatalf("prepare tx1: %d", code)
	}
	if code, _ := post(t, pa.URL+"/prepare", `{"txId":"tx2","payload":{"resource":"r1"}}`); code != http.StatusConflict {
		t.Fatalf("prepare tx2 on locked resource: want 409, got %d", code)
	}
	if code, _ := post(t, pa.URL+"/commit", `{"txId":"tx1"}`); code != http.StatusOK {
		t.Fatalf("commit tx1: %d", code)
	}
	if code, _ := post(t, pa.URL+"/prepare", `{"txId":"tx2","payload":{"resource":"r1"}}`); code != http.StatusOK {
		t.Fatalf("prepare tx2 after release: want 200, got %d", code)
	}
}

// Coordinator crashed AFTER the COMMIT decision was durable: on restart it
// must re-deliver COMMIT to all participants.
func TestCoordinatorRecoveryAfterCommitDecision(t *testing.T) {
	dir := t.TempDir()
	walPath := filepath.Join(dir, "c.wal")

	// Pre-seed the coordinator log as it would look after crashing between
	// "decision fsynced" and "participants notified".
	w, _, err := OpenWAL(walPath)
	if err != nil {
		t.Fatal(err)
	}
	_, pa := startParticipant(t, filepath.Join(dir, "a.wal"), "")
	_, pb := startParticipant(t, filepath.Join(dir, "b.wal"), "")
	parts := []string{pa.URL, pb.URL}
	w.Append(Record{TxID: "txR", State: StatePreparing, Participants: parts, Payload: json.RawMessage(`{"resource":"x"}`)})
	w.Append(Record{TxID: "txR", State: StateCommit})
	w.Close()
	// The participants had voted yes before the crash.
	for _, p := range parts {
		if code, resp := post(t, p+"/prepare", `{"txId":"txR","payload":{"resource":"x"}}`); code != http.StatusOK {
			t.Fatalf("prepare: %d %s", code, resp)
		}
	}

	_, coord := startCoordinator(t, walPath)
	for _, p := range parts {
		p := p
		waitFor(t, p+" COMMITTED", 5*time.Second, func() bool {
			return txState(t, p, "txR") == StateCommitted
		})
	}
	waitFor(t, "coordinator DONE", 5*time.Second, func() bool {
		return getJSON(t, coord.URL+"/tx/txR")["state"] == StateDone
	})
}

// Coordinator crashed BEFORE any decision was durable: on restart it must
// abort the transaction, and prepared participants must learn the abort.
func TestCoordinatorRecoveryWithoutDecision(t *testing.T) {
	dir := t.TempDir()
	walPath := filepath.Join(dir, "c.wal")

	_, pa := startParticipant(t, filepath.Join(dir, "a.wal"), "")
	_, pb := startParticipant(t, filepath.Join(dir, "b.wal"), "")
	parts := []string{pa.URL, pb.URL}
	for _, p := range parts {
		post(t, p+"/prepare", `{"txId":"txR","payload":{}}`)
	}
	w, _, err := OpenWAL(walPath)
	if err != nil {
		t.Fatal(err)
	}
	w.Append(Record{TxID: "txR", State: StatePreparing, Participants: parts, Payload: json.RawMessage(`{}`)})
	w.Close()

	// Participants must re-ask the coordinator, so point them at it: create
	// the coordinator first to learn its URL, then recreate participants.
	// (Here we cheat slightly: restart participants with the coord URL.)
	_, coord := startCoordinator(t, walPath)
	pa2, paSrv := startParticipant(t, filepath.Join(dir, "a.wal"), coord.URL)
	_ = pa2
	pb2, pbSrv := startParticipant(t, filepath.Join(dir, "b.wal"), coord.URL)
	_ = pb2

	for _, p := range []string{paSrv.URL, pbSrv.URL} {
		p := p
		waitFor(t, p+" ABORTED", 5*time.Second, func() bool {
			return txState(t, p, "txR") == StateAborted
		})
	}
	v := getJSON(t, coord.URL+"/tx/txR")
	if v["decision"] != StateAbort {
		t.Fatalf("coordinator decision: want ABORT, got %v", v)
	}
}

// A PREPARED participant whose coordinator is unreachable must stay
// PREPARED (blocked) and must NOT abort on its own, no matter how long it
// waits. When the coordinator returns, the participant learns the decision.
func TestParticipantNeverSelfAborts(t *testing.T) {
	dir := t.TempDir()

	// Coordinator that is gone (closed before the participant starts).
	dead := httptest.NewServer(http.NewServeMux())
	deadURL := dead.URL
	dead.Close()

	p, ps := startParticipant(t, filepath.Join(dir, "a.wal"), deadURL)
	_ = p
	if code, _ := post(t, ps.URL+"/prepare", `{"txId":"txB","payload":{"resource":"r"}}`); code != http.StatusOK {
		t.Fatalf("prepare: %d", code)
	}

	// Wait far longer than any poll interval: still PREPARED, never aborted.
	time.Sleep(500 * time.Millisecond)
	if s := txState(t, ps.URL, "txB"); s != StatePrepared {
		t.Fatalf("participant must stay PREPARED while coordinator is down, got %s", s)
	}
	v := getJSON(t, ps.URL+"/status")
	tx := v["txs"].(map[string]any)["txB"].(map[string]any)
	if tx["blocked"] != true {
		t.Fatalf("prepared tx should report blocked=true: %v", tx)
	}

	// Coordinator comes back; it had decided COMMIT before its own crash.
	walPath := filepath.Join(dir, "c.wal")
	w, _, _ := OpenWAL(walPath)
	w.Append(Record{TxID: "txB", State: StatePreparing, Participants: []string{ps.URL}, Payload: json.RawMessage(`{"resource":"r"}`)})
	w.Append(Record{TxID: "txB", State: StateCommit})
	w.Close()
	_, coord := startCoordinator(t, walPath)
	_ = coord

	waitFor(t, "participant COMMITTED", 5*time.Second, func() bool {
		return txState(t, ps.URL, "txB") == StateCommitted
	})
}

// Commit/abort delivery is idempotent: duplicate decisions are safe.
func TestIdempotentDecisionDelivery(t *testing.T) {
	dir := t.TempDir()
	_, pa := startParticipant(t, filepath.Join(dir, "a.wal"), "")

	post(t, pa.URL+"/prepare", `{"txId":"tx1","payload":{}}`)
	for i := 0; i < 3; i++ {
		if code, _ := post(t, pa.URL+"/commit", `{"txId":"tx1"}`); code != http.StatusOK {
			t.Fatalf("commit #%d: %d", i, code)
		}
	}
	if code, _ := post(t, pa.URL+"/abort", `{"txId":"tx1"}`); code != http.StatusConflict {
		t.Fatalf("abort after commit: want 409, got %d", code)
	}
	if s := txState(t, pa.URL, "tx1"); s != StateCommitted {
		t.Fatalf("want COMMITTED, got %s", s)
	}
}
