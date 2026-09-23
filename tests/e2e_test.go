package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ximbox/internal/cryptoenvelope"
)

// e2e builds and drives the REAL server binary over HTTP. This is the
// crash/restart scenario: a process genuinely exits mid-execution and a new
// process completes the prefix on startup.
type e2e struct {
	t       *testing.T
	h       *harness
	binary  string
	addr    string
	dbURL   string
	cancel  context.CancelFunc
	logFile *os.File
	client  *http.Client
}

func newE2E(t *testing.T, h *harness) *e2e {
	t.Helper()
	dir := t.TempDir()
	binary := filepath.Join(dir, "ximbox-server")
	build := exec.Command("go", "build", "-o", binary, "ximbox/cmd/server")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build server binary: %v\n%s", err, out)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	return &e2e{
		t:      t,
		h:      h,
		binary: binary,
		addr:   addr,
		dbURL:  testDBURL(),
		client: &http.Client{Timeout: 10 * time.Second},
	}
}

// startExpectingCrash launches the server armed with a crash hook. The
// process is expected to exit during startup recovery (before it serves
// traffic), so no readiness wait is performed. Returns a channel that
// receives the process exit result.
func (e *e2e) startExpectingCrash(crashAt string) <-chan error {
	e.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	e.cancel = cancel

	logFile, err := os.CreateTemp("", "ximbox-e2e-crash-*.log")
	if err != nil {
		e.t.Fatal(err)
	}
	e.logFile = logFile

	cmd := exec.CommandContext(ctx, e.binary)
	cmd.Env = append(os.Environ(),
		"XIMBOX_HTTP_ADDR="+e.addr,
		"XIMBOX_DATABASE_URL="+e.dbURL,
		"XIMBOX_CRASH_AT="+crashAt,
	)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		e.t.Fatalf("start crash-armed server: %v", err)
	}
	ch := make(chan error, 1)
	go func() { ch <- cmd.Wait() }()
	return ch
}

// start launches the server; crashAt="chain/ch/seq" arms pre-commit crash.
// The returned waitFn blocks until the process exits.
func (e *e2e) start(crashAt string) (waitFn func() error) {
	e.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	e.cancel = cancel

	logFile, err := os.CreateTemp("", "ximbox-e2e-*.log")
	if err != nil {
		e.t.Fatal(err)
	}
	e.logFile = logFile

	cmd := exec.CommandContext(ctx, e.binary)
	cmd.Env = append(os.Environ(),
		"XIMBOX_HTTP_ADDR="+e.addr,
		"XIMBOX_DATABASE_URL="+e.dbURL,
	)
	if crashAt != "" {
		cmd.Env = append(cmd.Env, "XIMBOX_CRASH_AT="+crashAt)
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		e.t.Fatalf("start server: %v", err)
	}
	e.waitForHealthy()
	return cmd.Wait
}

func (e *e2e) stop() {
	if e.cancel != nil {
		e.cancel()
	}
}

func (e *e2e) waitForHealthy() {
	e.t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	url := "http://" + e.addr + "/healthz"
	for time.Now().Before(deadline) {
		resp, err := e.client.Get(url)
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	out, _ := os.ReadFile(e.logFile.Name())
	e.t.Fatalf("server never became healthy; log:\n%s", string(out))
}

func (e *e2e) post(path string, body any) (int, map[string]any) {
	e.t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		e.t.Fatal(err)
	}
	resp, err := e.client.Post("http://"+e.addr+path, "application/json", bytes.NewReader(raw))
	if err != nil {
		e.t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var parsed map[string]any
	_ = json.Unmarshal(data, &parsed)
	if parsed == nil {
		parsed = map[string]any{"raw": string(data)}
	}
	return resp.StatusCode, parsed
}

func (e *e2e) proposeBlockHTTP(chain string, n int, parent string) string {
	e.t.Helper()
	hash := cryptoenvelope.SimBlockHash(chain, int64(n), parent, "")
	status, _ := e.post("/v1/blocks", map[string]any{
		"source_chain": chain, "height": n, "hash": hash, "parent_hash": parent,
	})
	if status != http.StatusCreated {
		e.t.Fatalf("propose block %d status=%d", n, status)
	}
	return hash
}

func (e *e2e) confirmBlockHTTP(chain, hash string) {
	e.t.Helper()
	status, body := e.post("/v1/blocks/confirm", map[string]any{
		"source_chain": chain, "hash": hash,
	})
	if status != http.StatusOK {
		e.t.Fatalf("confirm block status=%d body=%v", status, body)
	}
}

// proposeBlockHTTPBare / confirmBlockHTTPBare prepare chain state directly in
// the database via the harness service (used when no server is running yet).
func (e *e2e) proposeBlockHTTPBare(chain string, n int, parent string) string {
	e.t.Helper()
	hash := cryptoenvelope.SimBlockHash(chain, int64(n), parent, "")
	if err := e.h.svc.ProposeBlock(e.h.ctx, chain, hash, parent, int64(n)); err != nil {
		e.t.Fatalf("bare propose block %d: %v", n, err)
	}
	return hash
}

func (e *e2e) confirmBlockHTTPBare(chain, hash string) {
	e.t.Helper()
	if _, err := e.h.svc.ConfirmBlock(e.h.ctx, chain, hash); err != nil {
		e.t.Fatalf("bare confirm block: %v", err)
	}
}

// e2eKeys signs with deterministic fixtures but needs block hashes derived
// exactly like the server's accepted blocks.
var e2eKeys = cryptoenvelope.TrustedFixtures()

func (e *e2e) postSignedMessage(chain, channel string, seq uint64, blockHash string, to string, amount int64) (int, map[string]any) {
	body := fmt.Sprintf(`{"type":"transfer","to":%q,"amount":%d}`, to, amount)
	env, err := cryptoenvelope.Sign(e2eKeys[chain].Private, chain, channel, blockHash, seq,
		json.RawMessage(body))
	if err != nil {
		e.t.Fatal(err)
	}
	raw, _ := json.Marshal(env)
	resp, err := e.client.Post("http://"+e.addr+"/v1/messages", "application/json", bytes.NewReader(raw))
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var parsed map[string]any
	_ = json.Unmarshal(data, &parsed)
	return resp.StatusCode, parsed
}

func (e *e2e) getBalance(addr string) int64 {
	resp, err := e.client.Get("http://" + e.addr + "/v1/accounts")
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Accounts []struct {
			Address string `json:"address"`
			Balance int64  `json:"balance"`
		} `json:"accounts"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		e.t.Fatal(err)
	}
	for _, a := range out.Accounts {
		if a.Address == addr {
			return a.Balance
		}
	}
	return 0
}

func (e *e2e) messageStatus(chain, channel string, seq int64) string {
	url := fmt.Sprintf("http://%s/v1/messages?source_chain=%s&channel=%s",
		e.addr, chain, channel)
	resp, err := e.client.Get(url)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Messages []struct {
			Sequence int64  `json:"sequence"`
			Status   string `json:"status"`
		} `json:"messages"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		e.t.Fatal(err)
	}
	for _, m := range out.Messages {
		if m.Sequence == seq {
			return m.Status
		}
	}
	return ""
}

// TestCrashBeforeCommitAndRestart drives a genuine process crash between the
// SQL effects of an execution and its commit, then asserts a fresh process
// resumes the contiguous prefix exactly once on startup.
//
// Layout:
//   - seq0 already executed and committed before the crashed run.
//   - seq1 is pending on a final block and is the crash target.
//   - seq2 is pending on a final block but beyond the seq1 gap (staged).
//   - Run A (crash armed for seq1): startup recovery applies seq1's SQL, then
//     exits(99) BEFORE committing. The process never serves HTTP.
//   - Run B (no hook): startup recovery rolls nothing forward a second time
//     for seq0, commits seq1 exactly once, then fills the gap and runs seq2.
func TestCrashBeforeCommitAndRestart(t *testing.T) {
	h := newHarness(t) // owns pool + truncate + migrate; closed by t.Cleanup

	e := newE2E(t, h)

	const chain, ch = cryptoenvelope.ChainA, "crash-channel"

	// Confirmed height-1 chain. block 2 is proposed while seq1/seq2 land, so
	// they stage as pending; then block 2 is marked final DIRECTLY in the
	// database (bypassing the service's post-confirmation advancement). This
	// reproduces the exact situation a restart must recover from: messages on
	// final blocks that the process has not executed yet.
	b1 := e.proposeBlockHTTPBare(chain, 1, cryptoenvelope.ZeroHash)
	e.confirmBlockHTTPBare(chain, b1)
	b2 := e.proposeBlockHTTPBare(chain, 2, b1)

	h.ingest(h.signMsgHash(chain, ch, 0, b1, transferBody("alice", 10))) // executed
	h.ingest(h.signMsgHash(chain, ch, 1, b2, transferBody("bob", 20)))   // pending: block proposed
	h.ingest(h.signMsgHash(chain, ch, 2, b2, transferBody("carol", 30))) // pending: behind seq1
	if _, err := h.pool.Exec(h.ctx,
		`UPDATE blocks SET status='final', finalized_at=now()
		  WHERE source_chain=$1 AND hash=$2`, chain, b2); err != nil {
		t.Fatalf("mark b2 final directly: %v", err)
	}

	if s := h.msgStatus(chain, ch, 0); s != "executed" {
		t.Fatalf("precondition seq0=%s want executed", s)
	}
	if s := h.msgStatus(chain, ch, 1); s != "pending" {
		t.Fatalf("precondition seq1=%s want pending", s)
	}
	if got := h.balance("alice"); got != 10 {
		t.Fatalf("precondition alice=%d want 10", got)
	}

	// Run A: armed to crash on seq1 during startup recovery.
	waitA := e.startExpectingCrash(
		fmt.Sprintf("source_chain=%s,channel=%s,sequence=1", chain, ch))
	crashLogPath := e.logFile.Name()

	select {
	case err := <-waitA:
		if exitErr, ok := err.(*exec.ExitError); !ok || exitErr.ExitCode() != 99 {
			t.Fatalf("expected exit code 99, got %v\nlog:\n%s", err, readLog(t, crashLogPath))
		}
	case <-time.After(20 * time.Second):
		t.Fatal("crashed process did not exit in time")
	}

	// Durable state with the process dead: seq1's pre-commit transaction must
	// have rolled back completely; seq0 stays committed; seq2 stays staged.
	if got := h.balance("bob"); got != 0 {
		t.Fatalf("bob=%d: crashed seq1 tx must have rolled back", got)
	}
	executed, err := h.st.HasExecution(context.Background(), chain, ch, 1)
	if err != nil {
		t.Fatal(err)
	}
	if executed {
		t.Fatal("seq1 must not be in the execution ledger after pre-commit crash")
	}
	if s := h.msgStatus(chain, ch, 1); s != "pending" {
		t.Fatalf("seq1=%s want pending after crash rollback", s)
	}
	if s := h.msgStatus(chain, ch, 2); s != "pending" {
		t.Fatalf("seq2=%s want still staged", s)
	}

	// Run B: clean restart without the crash hook. Startup recovery must
	// commit seq1 exactly once and then advance over staged seq2 (gap fill).
	waitB := e.start("")
	defer func() {
		e.stop()
		_ = waitB
	}()

	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if e.messageStatus(chain, ch, 1) == "executed" &&
			e.messageStatus(chain, ch, 2) == "executed" {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if e.messageStatus(chain, ch, 0) != "executed" ||
		e.messageStatus(chain, ch, 1) != "executed" ||
		e.messageStatus(chain, ch, 2) != "executed" {
		t.Fatalf("post-restart statuses: %q %q %q",
			e.messageStatus(chain, ch, 0),
			e.messageStatus(chain, ch, 1),
			e.messageStatus(chain, ch, 2))
	}
	if a, b, c := e.getBalance("alice"), e.getBalance("bob"), e.getBalance("carol"); a != 10 || b != 20 || c != 30 {
		t.Fatalf("exactly-once violated: alice=%d bob=%d carol=%d", a, b, c)
	}

	// Idempotent extra restart: nothing may be applied a second time.
	e.stop()
	_ = waitB
	waitC := e.start("")
	defer func() { e.stop(); _ = waitC }()
	time.Sleep(500 * time.Millisecond)
	if a, b, c := e.getBalance("alice"), e.getBalance("bob"), e.getBalance("carol"); a != 10 || b != 20 || c != 30 {
		t.Fatalf("balances changed on restart replay: alice=%d bob=%d carol=%d", a, b, c)
	}
	if !strings.Contains(readLog(t, crashLogPath), "CRASH INJECTION") {
		t.Fatalf("run-A log should record the real crash; log:\n%s", readLog(t, crashLogPath))
	}
}

func readLog(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(b)
}
