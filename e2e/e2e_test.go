// Package e2e contains end-to-end tests that exercise the REAL system:
// application HTTP server + in-process fake gateway over real TCP
// connections, including client disconnects and hard process crashes.
package e2e

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"idemresp/internal/client"
	"idemresp/internal/clock"
	"idemresp/internal/gateway"
	"idemresp/internal/idem"
	"idemresp/internal/server"
	"idemresp/internal/txn"
)

// ---------------------------------------------------------------------------
// Local (in-process) stack
// ---------------------------------------------------------------------------

type stack struct {
	appBase string
	gwBase  string
	db      *txn.DB
	appSrv  *http.Server
	gwSrv   *http.Server
	gwS     *gateway.Server
	dir     string
}

func startStack(t *testing.T, lease time.Duration) *stack {
	t.Helper()
	dir := t.TempDir()
	db, err := txn.Open(filepath.Join(dir, "wal"), clock.Real{})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}

	gwS := gateway.NewServer(nil)
	gwLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("gateway listen: %v", err)
	}
	gwSrv := &http.Server{Handler: gwS.Handler(), ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = gwSrv.Serve(gwLn) }()

	gwClient := client.New("http://"+gwLn.Addr().String(), 2*time.Second)
	svc := idem.New(idem.Config{
		DB:               db,
		GW:               gwClient,
		Clk:              clock.Real{},
		Lease:            lease,
		AmbiguousRetries: 1,
		Logf:             func(string, ...any) {},
	})

	appLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("app listen: %v", err)
	}
	appSrv := &http.Server{
		Handler:           server.New(server.Deps{Service: svc, DB: db, AllowCrash: false}),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() { _ = appSrv.Serve(appLn) }()

	s := &stack{
		appBase: "http://" + appLn.Addr().String(),
		gwBase:  "http://" + gwLn.Addr().String(),
		db:      db,
		appSrv:  appSrv,
		gwSrv:   gwSrv,
		gwS:     gwS,
		dir:     dir,
	}
	t.Cleanup(s.close)
	s.waitHealthy(t)
	return s
}

func (s *stack) close() {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = s.appSrv.Shutdown(ctx)
	_ = s.gwSrv.Shutdown(ctx)
	_ = s.db.Close()
}

func (s *stack) waitHealthy(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(s.appBase + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("app did not become healthy")
}

// postResult is the outcome of one raw client POST.
type postResult struct {
	status int
	body   []byte
	err    error
	header http.Header
}

func (s *stack) postOrder(t *testing.T, key string, body string, headers map[string]string, timeout time.Duration) postResult {
	t.Helper()
	ctx := context.Background()
	var cancel context.CancelFunc
	if timeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.appBase+"/v1/orders", bytes.NewBufferString(body))
	if err != nil {
		return postResult{err: err}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return postResult{err: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	h := resp.Header.Clone()
	return postResult{status: resp.StatusCode, body: raw, header: h}
}

func (s *stack) ledgerCountForKey(t *testing.T, key string) int {
	t.Helper()
	resp, err := http.Get(s.appBase + "/v1/ledger?key=" + key)
	if err != nil {
		t.Fatalf("ledger: %v", err)
	}
	defer resp.Body.Close()
	var m struct {
		Count int `json:"count"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		t.Fatalf("decode ledger: %v", err)
	}
	return m.Count
}

func (s *stack) gatewayChargeCount(t *testing.T) int {
	t.Helper()
	resp, err := http.Get(s.gwBase + "/gateway/metrics")
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	defer resp.Body.Close()
	var m gateway.Metrics
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		t.Fatalf("decode metrics: %v", err)
	}
	return m.Charges
}

func (s *stack) resetGateway(t *testing.T) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, s.gwBase+"/gateway/reset", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("gateway reset: %v", err)
	}
	_ = resp.Body.Close()
}

// pollReplay retries the key until it gets the completed 201 response.
func (s *stack) pollReplay(t *testing.T, key, body string, timeout time.Duration) postResult {
	t.Helper()
	deadline := time.Now().Add(timeout)
	var last postResult
	for time.Now().Before(deadline) {
		last = s.postOrder(t, key, body, nil, 2*time.Second)
		if last.status == http.StatusCreated {
			return last
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("key %s never completed; last=%+v body=%s", key, last, last.body)
	return postResult{}
}

const orderBody = `{"amount":1000,"currency":"USD","reference":"invoice-42"}`

var _ = sync.Once{}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

func TestE2E_HappyThenReplay(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-happy"

	r := s.postOrder(t, key, orderBody, nil, 3*time.Second)
	if r.status != http.StatusCreated {
		t.Fatalf("create: %d %s err=%v", r.status, r.body, r.err)
	}
	if r.header.Get("Idempotent-Replay") != "" {
		t.Fatal("first response must not be marked replay")
	}
	first := string(r.body)

	r2 := s.postOrder(t, key, orderBody, nil, 3*time.Second)
	if r2.status != http.StatusCreated || r2.header.Get("Idempotent-Replay") != "true" {
		t.Fatalf("replay: status=%d replay=%q", r2.status, r2.header.Get("Idempotent-Replay"))
	}
	if string(r2.body) != first {
		t.Fatalf("replay body differs:\n%s\n%s", first, r2.body)
	}
	if got := s.ledgerCountForKey(t, key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1", got)
	}
}

func TestE2E_ConflictOnDifferentBody(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-conflict"
	if r := s.postOrder(t, key, orderBody, nil, 3*time.Second); r.status != 201 {
		t.Fatalf("setup: %d", r.status)
	}
	r := s.postOrder(t, key, `{"amount":9999,"currency":"USD"}`, nil, 3*time.Second)
	if r.status != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %s", r.status, r.body)
	}
	if got := s.ledgerCountForKey(t, key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}
}

func TestE2E_ConcurrentRetriesOneSideEffect(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-concurrent"
	const n = 16

	// Slow the gateway so all requests pile in while the first is running.
	results := make([]postResult, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			// Small staggered start within a tight window.
			time.Sleep(time.Duration(i) * 2 * time.Millisecond)
			results[i] = s.postOrder(t, key, orderBody, nil, 5*time.Second)
		}(i)
	}
	wg.Wait()

	created, inProgress, replayed := 0, 0, 0
	for _, r := range results {
		switch {
		case r.err != nil:
			t.Fatalf("unexpected transport error: %v", r.err)
		case r.status == http.StatusCreated && r.header.Get("Idempotent-Replay") == "true":
			replayed++
		case r.status == http.StatusCreated:
			created++
		case r.status == http.StatusConflict:
			inProgress++
		default:
			t.Fatalf("unexpected status %d body=%s", r.status, r.body)
		}
	}
	if created != 1 {
		t.Fatalf("fresh executions = %d, want exactly 1 (inProgress=%d replayed=%d)",
			created, inProgress, replayed)
	}

	// Every client retries: all must now get the identical replay.
	bodies := make(map[string]int)
	for i := 0; i < n; i++ {
		r := s.postOrder(t, key, orderBody, nil, 3*time.Second)
		if r.status != http.StatusCreated || r.header.Get("Idempotent-Replay") != "true" {
			t.Fatalf("retry %d: status=%d replay=%q", i, r.status, r.header.Get("Idempotent-Replay"))
		}
		bodies[string(r.body)]++
	}
	if len(bodies) != 1 {
		t.Fatalf("replayed responses were not identical: %d variants", len(bodies))
	}
	if got := s.ledgerCountForKey(t, key); got != 1 {
		t.Fatalf("local side effects = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1", got)
	}
}

func TestE2E_DisconnectBeforeCommit(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-disconnect-before"

	// Gateway succeeds quickly, but the server waits 1.2s BEFORE the
	// atomic commit. The client gives up after 300 ms and disconnects.
	r := s.postOrder(t, key, orderBody,
		map[string]string{"X-Pre-Commit-Delay": "1200ms"}, 300*time.Millisecond)
	if r.err == nil {
		t.Fatalf("expected client-side timeout, got status %d", r.status)
	}

	// While the request is still in flight, a duplicate must be told
	// "processing" rather than execute twice.
	dup := s.postOrder(t, key, orderBody, nil, time.Second)
	if dup.status != http.StatusConflict {
		t.Fatalf("duplicate during processing: status=%d body=%s", dup.status, dup.body)
	}

	// The detached handler commits anyway; retry then replays.
	final := s.pollReplay(t, key, orderBody, 5*time.Second)
	if final.header.Get("Idempotent-Replay") != "true" {
		t.Fatal("final response after disconnect must be a replay")
	}
	if got := s.ledgerCountForKey(t, key); got != 1 {
		t.Fatalf("local side effects = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1", got)
	}
}

func TestE2E_DisconnectAfterCommit(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-disconnect-after"

	// Commit happens immediately; the response is held for 1.2s. The
	// side effect is already durable when the client disconnects.
	r := s.postOrder(t, key, orderBody,
		map[string]string{"X-Post-Commit-Delay": "1200ms"}, 300*time.Millisecond)
	if r.err == nil {
		t.Fatalf("expected client-side timeout, got status %d", r.status)
	}

	// Immediate retry replays the committed result; no re-execution.
	retry := s.postOrder(t, key, orderBody, nil, 3*time.Second)
	if retry.status != http.StatusCreated || retry.header.Get("Idempotent-Replay") != "true" {
		t.Fatalf("retry: status=%d replay=%q body=%s",
			retry.status, retry.header.Get("Idempotent-Replay"), retry.body)
	}
	if got := s.ledgerCountForKey(t, key); got != 1 {
		t.Fatalf("local side effects = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1", got)
	}
}

func TestE2E_KeyedAmbiguousTimeoutRecovers(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-ambiguous-keyed"

	// First gateway attempt charges then drops the connection; the service
	// auto-retries with the same end-to-end key, the gateway dedupes.
	r := s.postOrder(t, key, orderBody,
		map[string]string{"X-Fault": gateway.FaultResetAfterCharge}, 5*time.Second)
	if r.status != http.StatusCreated {
		t.Fatalf("status = %d body=%s err=%v", r.status, r.body, r.err)
	}
	if !strings.Contains(string(r.body), `"deduped": true`) {
		t.Fatalf("expected deduped retry result: %s", r.body)
	}
	if got := s.ledgerCountForKey(t, key); got != 1 {
		t.Fatalf("local side effects = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1 (deduplicated)", got)
	}
}

// TestE2E_KeylessAmbiguousIsNotExactlyOnce documents the BOUNDARY: with no
// end-to-end key forwarded, an ambiguous gateway result leaves a charge the
// local system cannot see or deduplicate. The service refuses to commit a
// local side effect or to blindly retry; reconciliation is required.
func TestE2E_KeylessAmbiguousIsNotExactlyOnce(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-ambiguous-keyless"

	r := s.postOrder(t, key, orderBody,
		map[string]string{
			"X-Fault":       gateway.FaultResetAfterCharge,
			"X-Forward-Key": "false",
		}, 5*time.Second)
	if r.status != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503: %s", r.status, r.body)
	}
	if !strings.Contains(string(r.body), "ambiguous_outcome") {
		t.Fatalf("body = %s", r.body)
	}
	// No LOCAL transactional side effect...
	if got := s.ledgerCountForKey(t, key); got != 0 {
		t.Fatalf("local side effects = %d, want 0", got)
	}
	// ...but the external gateway DID charge once, invisibly to us. This
	// is precisely why exactly-once for arbitrary external calls is not
	// guaranteed by a local idempotency layer alone.
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1 (invisible external effect)", got)
	}
}

func TestE2E_RestartReplaysPersistedResult(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-restart"
	r := s.postOrder(t, key, orderBody, nil, 3*time.Second)
	if r.status != 201 {
		t.Fatalf("create: %d %s", r.status, r.body)
	}
	first := string(r.body)
	walDir := filepath.Join(s.dir, "wal")

	// Stop the whole app (simulating a restart), reopen the same WAL.
	s.close()
	db2, err := txn.Open(walDir, clock.Real{})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	gwClient := client.New(s.gwBase, 2*time.Second)
	svc := idem.New(idem.Config{DB: db2, GW: gwClient, Lease: 30 * time.Second, AmbiguousRetries: 1})
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: server.New(server.Deps{Service: svc, DB: db2})}
	go func() { _ = srv.Serve(ln) }()
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
		_ = db2.Close()
	}()

	req, _ := http.NewRequest(http.MethodPost, "http://"+ln.Addr().String()+"/v1/orders",
		bytes.NewBufferString(orderBody))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("retry after restart: %v", err)
	}
	defer resp2.Body.Close()
	raw, _ := io.ReadAll(resp2.Body)
	if resp2.StatusCode != 201 || resp2.Header.Get("Idempotent-Replay") != "true" {
		t.Fatalf("post-restart replay: status=%d replay=%q", resp2.StatusCode, resp2.Header.Get("Idempotent-Replay"))
	}
	if string(raw) != first {
		t.Fatalf("post-restart body differs")
	}
}

// ---------------------------------------------------------------------------
// Subprocess hard-crash tests
// ---------------------------------------------------------------------------

const workerEnv = "IDEMRESP_CRASH_WORKER"

// TestCrashWorker is NOT a normal test: the E2E tests re-exec the test
// binary with -test.run pointing here. The worker starts a fresh app
// process sharing the parent's gateway and WAL directory, then waits for a
// signal; the real crash (os.Exit) fires from the X-Crash request hook.
func TestCrashWorker(t *testing.T) {
	if os.Getenv(workerEnv) != "1" {
		t.Skip("crash worker helper")
	}
	dir := os.Getenv("IDEMRESP_DATA_DIR")
	gwURL := os.Getenv("IDEMRESP_GW_URL")
	lease, _ := time.ParseDuration(os.Getenv("IDEMRESP_LEASE"))

	db, err := txn.Open(dir, clock.Real{})
	if err != nil {
		fmt.Fprintf(os.Stderr, "worker open db: %v\n", err)
		os.Exit(70)
	}
	gwClient := client.New(gwURL, 1500*time.Millisecond)
	svc := idem.New(idem.Config{
		DB: db, GW: gwClient, Clk: clock.Real{},
		Lease: lease, AmbiguousRetries: 1,
		Logf:  func(string, ...any) {},
		Crash: server.CrashFn, // real os.Exit(77)
	})
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintf(os.Stderr, "worker listen: %v\n", err)
		os.Exit(70)
	}
	srv := &http.Server{Handler: server.New(server.Deps{Service: svc, DB: db, AllowCrash: true})}
	go func() { _ = srv.Serve(ln) }()

	fmt.Printf("READY %s\n", ln.Addr().String())
	os.Stdout.Sync()

	// Stay alive until the crash hook (or the parent) terminates us.
	select {}
}

type workerProc struct {
	cmd  *proc
	addr string
}

func startWorker(t *testing.T, gwURL, walDir, lease string) *workerProc {
	t.Helper()
	args := []string{"-test.run=^TestCrashWorker$", "-test.count=1", "-test.v"}
	cmd := startTestBinary(args,
		workerEnv+"=1",
		"IDEMRESP_DATA_DIR="+walDir,
		"IDEMRESP_GW_URL="+gwURL,
		"IDEMRESP_LEASE="+lease,
	)
	t.Cleanup(cmd.kill)
	line, err := cmd.waitReady()
	if err != nil {
		t.Fatalf("worker did not become ready: %v", err)
	}
	parts := strings.SplitN(strings.TrimSpace(line), " ", 2)
	if len(parts) != 2 || parts[0] != "READY" {
		t.Fatalf("bad worker hello: %q", line)
	}
	return &workerProc{cmd: cmd, addr: parts[1]}
}

func TestE2E_CrashBeforeCommit(t *testing.T) {
	s := startStack(t, 250*time.Millisecond) // short lease for crash recovery
	key := "e2e-crash-before"
	walDir := filepath.Join(s.dir, "wal2")

	w1 := startWorker(t, s.gwBase, walDir, "250ms")

	// The worker charges at the gateway, then hard-exits BEFORE the local
	// commit. The client sees a broken connection.
	req, _ := http.NewRequest(http.MethodPost, "http://"+w1.addr+"/v1/orders",
		bytes.NewBufferString(orderBody))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	req.Header.Set("X-Crash", idem.CrashBeforeCommit)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		_ = resp.Body.Close()
		t.Fatal("expected connection failure from crashing worker")
	}
	if code := w1.cmd.waitExit(5 * time.Second); code != 77 {
		t.Fatalf("worker exit code = %d, want 77", code)
	}

	// Gateway already charged (keyed); no local commit exists yet.
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1", got)
	}

	// Restart as a fresh process using the same WAL. The processing claim
	// is within lease at first -> 409; after the lease the claim can be
	// reclaimed and the keyed gateway call deduplicates.
	w2 := startWorker(t, s.gwBase, walDir, "250ms")
	defer w2.cmd.kill()

	// Past the 250ms lease, retry is allowed to re-execute.
	time.Sleep(300 * time.Millisecond)
	r := postPlain(t, "http://"+w2.addr, key, orderBody, nil)
	if r.status != http.StatusCreated {
		t.Fatalf("retry after crash: status=%d body=%s", r.status, r.body)
	}
	if !strings.Contains(string(r.body), `"deduped": true`) {
		t.Fatalf("expected gateway-deduplicated charge: %s", r.body)
	}

	// A third retry replays locally.
	r2 := postPlain(t, "http://"+w2.addr, key, orderBody, nil)
	if r2.status != 201 || r2.header.Get("Idempotent-Replay") != "true" {
		t.Fatalf("replay: status=%d replay=%q", r2.status, r2.header.Get("Idempotent-Replay"))
	}
	if got := ledgerOverHTTP(t, "http://"+w2.addr, key); got != 1 {
		t.Fatalf("local side effects = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges after recovery = %d, want 1", got)
	}
}

func TestE2E_CrashAfterCommit(t *testing.T) {
	s := startStack(t, 30*time.Second)
	key := "e2e-crash-after"
	walDir := filepath.Join(s.dir, "wal3")

	w1 := startWorker(t, s.gwBase, walDir, "30s")
	req, _ := http.NewRequest(http.MethodPost, "http://"+w1.addr+"/v1/orders",
		bytes.NewBufferString(orderBody))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	req.Header.Set("X-Crash", idem.CrashAfterCommit)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		_ = resp.Body.Close()
		t.Fatal("expected connection failure from crashing worker")
	}
	if code := w1.cmd.waitExit(5 * time.Second); code != 77 {
		t.Fatalf("worker exit code = %d, want 77", code)
	}

	// Restart: the commit survived the crash; retry must replay with no
	// second gateway charge and no second ledger row.
	w2 := startWorker(t, s.gwBase, walDir, "30s")
	defer w2.cmd.kill()

	r := postPlain(t, "http://"+w2.addr, key, orderBody, nil)
	if r.status != http.StatusCreated || r.header.Get("Idempotent-Replay") != "true" {
		t.Fatalf("post-crash replay: status=%d replay=%q body=%s",
			r.status, r.header.Get("Idempotent-Replay"), r.body)
	}
	if got := ledgerOverHTTP(t, "http://"+w2.addr, key); got != 1 {
		t.Fatalf("local side effects = %d, want 1", got)
	}
	if got := s.gatewayChargeCount(t); got != 1 {
		t.Fatalf("gateway charges = %d, want 1", got)
	}
}

func postPlain(t *testing.T, base, key, body string, headers map[string]string) postResult {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, base+"/v1/orders", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := (&http.Client{Timeout: 5 * time.Second}).Do(req)
	if err != nil {
		return postResult{err: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return postResult{status: resp.StatusCode, body: raw, header: resp.Header.Clone()}
}

func ledgerOverHTTP(t *testing.T, base, key string) int {
	t.Helper()
	resp, err := http.Get(base + "/v1/ledger?key=" + key)
	if err != nil {
		t.Fatalf("ledger: %v", err)
	}
	defer resp.Body.Close()
	var m struct {
		Count int `json:"count"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return m.Count
}

var _ = fmt.Sprintf
