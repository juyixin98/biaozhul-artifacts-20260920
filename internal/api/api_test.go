package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"idempotentsave/internal/auditclient"
	"idempotentsave/internal/clock"
	"idempotentsave/internal/fakeaudit"
	"idempotentsave/internal/service"
	"idempotentsave/internal/store"
)

// oneShotReader 阻止 net/http 在连接被关闭时自动重放请求。
type oneShotReader struct{ r io.Reader }

func (o *oneShotReader) Read(p []byte) (int, error) { return o.r.Read(p) }

type testEnv struct {
	biz     *httptest.Server
	audit   *fakeaudit.Server
	auditTS *httptest.Server
	store   *store.Store
	clk     *clock.Fake
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	clk := clock.NewFake()
	st, err := store.Open(store.Options{
		Dir: t.TempDir(), PendingTTL: time.Minute, Now: clk.Now,
	})
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	audit := fakeaudit.New()
	auditTS := httptest.NewServer(audit.Handler())
	t.Cleanup(auditTS.Close)

	svc := service.New(st, auditclient.New(auditTS.URL), clk)
	srv := New(Config{
		Store: st, Service: svc, AuditBaseURL: auditTS.URL,
		Clock: clk, WaitMax: 800 * time.Millisecond, RealClock: false,
	})
	biz := httptest.NewServer(srv.Handler())
	t.Cleanup(biz.Close)

	return &testEnv{biz: biz, audit: audit, auditTS: auditTS, store: st, clk: clk}
}

// closeAudit 关闭外部假审计服务，模拟其不可用。
func (e *testEnv) closeAudit() { e.auditTS.Close() }

func (e *testEnv) deposit(t *testing.T, key string, body string, headers map[string]string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("deposit transport error: %v", err)
	}
	return resp
}

func bodyString(t *testing.T, resp *http.Response) string {
	t.Helper()
	b, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return string(b)
}

func TestDepositCreateThenReplay(t *testing.T) {
	e := newTestEnv(t)
	body := `{"account":"alice","amount":100}`

	r1 := e.deposit(t, "k1", body, nil)
	if r1.StatusCode != http.StatusOK {
		t.Fatalf("first status=%d body=%s", r1.StatusCode, bodyString(t, r1))
	}
	if r1.Header.Get("Idempotency-Status") != "created" {
		t.Fatalf("first should be created, got %q", r1.Header.Get("Idempotency-Status"))
	}
	var first map[string]any
	_ = json.Unmarshal([]byte(bodyString(t, r1)), &first)
	if bal, _ := first["balance_after"].(float64); bal != 100 {
		t.Fatalf("first balance_after=%v want 100 (must be computed inside the commit)", bal)
	}

	r2 := e.deposit(t, "k1", body, nil)
	if r2.StatusCode != http.StatusOK {
		t.Fatalf("replay status=%d", r2.StatusCode)
	}
	if r2.Header.Get("Idempotency-Status") != "replayed" {
		t.Fatalf("replay header = %q, want replayed", r2.Header.Get("Idempotency-Status"))
	}
	var second map[string]any
	_ = json.Unmarshal([]byte(bodyString(t, r2)), &second)
	if first["tx_id"] != second["tx_id"] {
		t.Fatalf("tx_id changed on replay: %v vs %v", first["tx_id"], second["tx_id"])
	}
	if bal, _ := second["balance_after"].(float64); bal != 100 {
		t.Fatalf("replayed balance_after=%v want 100 (saved snapshot, not zero/current)", bal)
	}
	if e.audit.Calls() != 1 {
		t.Fatalf("audit calls=%d, want 1 (replay must not re-execute)", e.audit.Calls())
	}
	if len(e.store.Ledger()) != 1 || e.store.Balance("alice") != 100 {
		t.Fatalf("ledger=%v balance=%d", e.store.Ledger(), e.store.Balance("alice"))
	}
}

// 重放保存的是"提交当时"的余额快照：之后再存入账，旧响应的快照不得被改写。
func TestReplayFreezesBalanceSnapshotAtCommit(t *testing.T) {
	e := newTestEnv(t)
	r1 := e.deposit(t, "k1", `{"account":"alice","amount":100}`, nil)
	r2 := e.deposit(t, "k2", `{"account":"alice","amount":50}`, nil)
	if r1.StatusCode != 200 || r2.StatusCode != 200 {
		t.Fatalf("status %d/%d", r1.StatusCode, r2.StatusCode)
	}
	var resp2 map[string]any
	_ = json.Unmarshal([]byte(bodyString(t, r2)), &resp2)
	if bal, _ := resp2["balance_after"].(float64); bal != 150 {
		t.Fatalf("second balance_after=%v want 150", bal)
	}
	// 重放第一笔：必须仍是 100（快照冻结），而不是当前余额 150
	rep := e.deposit(t, "k1", `{"account":"alice","amount":100}`, nil)
	var snap map[string]any
	_ = json.Unmarshal([]byte(bodyString(t, rep)), &snap)
	if bal, _ := snap["balance_after"].(float64); bal != 100 {
		t.Fatalf("replayed first balance_after=%v want frozen 100", bal)
	}
	if e.store.Balance("alice") != 150 {
		t.Fatalf("current balance=%d want 150", e.store.Balance("alice"))
	}
	if len(e.store.Ledger()) != 2 {
		t.Fatalf("ledger=%d want 2", len(e.store.Ledger()))
	}
}

func TestDepositConflictDifferentBody(t *testing.T) {
	e := newTestEnv(t)
	e.deposit(t, "k", `{"account":"a","amount":10}`, nil)
	r := e.deposit(t, "k", `{"account":"a","amount":20}`, nil)
	if r.StatusCode != http.StatusConflict {
		t.Fatalf("status=%d, want 409", r.StatusCode)
	}
	if len(e.store.Ledger()) != 1 {
		t.Fatal("conflict must not add effects")
	}
}

func TestDepositRequiresKey(t *testing.T) {
	e := newTestEnv(t)
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits", bytes.NewBufferString(`{"account":"a","amount":1}`))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", resp.StatusCode)
	}
}

func TestValidationFailureIsStoredAndReplayed(t *testing.T) {
	e := newTestEnv(t)
	body := `{"account":"a","amount":-5}`
	r1 := e.deposit(t, "k", body, nil)
	if r1.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422 body=%s", r1.StatusCode, bodyString(t, r1))
	}
	r2 := e.deposit(t, "k", body, nil)
	if r2.StatusCode != http.StatusUnprocessableEntity ||
		r2.Header.Get("Idempotency-Status") != "replayed" {
		t.Fatalf("validation failure must be replayed: status=%d replay=%q",
			r2.StatusCode, r2.Header.Get("Idempotency-Status"))
	}
	if len(e.store.Ledger()) != 0 {
		t.Fatal("rejected request must have no effect")
	}
}

func TestInProgressDuplicateWaitsAndReplays(t *testing.T) {
	e := newTestEnv(t)
	body := `{"account":"bob","amount":33}`
	hdr := map[string]string{"X-Fault-Delay-Before-Commit-Ms": "400"}

	var first *http.Response
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); first = e.deposit(t, "k-ip", body, hdr) }()
	time.Sleep(150 * time.Millisecond)
	var second *http.Response
	go func() { defer wg.Done(); second = e.deposit(t, "k-ip", body, nil) }()
	wg.Wait()

	if first.StatusCode != http.StatusOK {
		t.Fatalf("first status=%d", first.StatusCode)
	}
	if second.StatusCode != http.StatusOK ||
		second.Header.Get("Idempotency-Status") != "replayed" {
		t.Fatalf("duplicate status=%d replay=%q, want 200 replayed",
			second.StatusCode, second.Header.Get("Idempotency-Status"))
	}
	if bodyString(t, first) == "" || bodyString(t, second) == "" {
		t.Fatal("both responses must have bodies")
	}
	if e.audit.Calls() != 1 || len(e.store.Ledger()) != 1 {
		t.Fatalf("audit=%d ledger=%d, want 1/1", e.audit.Calls(), len(e.store.Ledger()))
	}
}

func TestExternalFailureReleasesSlotAndRetrySucceeds(t *testing.T) {
	e := newTestEnv(t)
	e.audit.FailNextN(1)
	body := `{"account":"carol","amount":70}`

	r1 := e.deposit(t, "k-ext", body, nil)
	if r1.StatusCode != http.StatusBadGateway {
		t.Fatalf("first status=%d want 502 body=%s", r1.StatusCode, bodyString(t, r1))
	}
	// 占位已释放，重试成功
	r2 := e.deposit(t, "k-ext", body, nil)
	if r2.StatusCode != http.StatusOK {
		t.Fatalf("retry status=%d body=%s", r2.StatusCode, bodyString(t, r2))
	}
	if len(e.store.Ledger()) != 1 || e.store.Balance("carol") != 70 {
		t.Fatalf("ledger=%v balance=%d", e.store.Ledger(), e.store.Balance("carol"))
	}
	if e.audit.Calls() != 2 {
		t.Fatalf("audit calls=%d, want 2 (external can be retried)", e.audit.Calls())
	}
}

func TestBeforeCommitDisconnectLeavesPending(t *testing.T) {
	e := newTestEnv(t)
	// 不使用默认会跟随重定向/复用连接的客户端；这里请求只需观察传输错误
	client := &http.Client{Timeout: 5 * time.Second}
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits",
		&oneShotReader{r: bytes.NewBufferString(`{"account":"d","amount":9}`)})
	req.Header.Set("Idempotency-Key", "k-disco")
	req.Header.Set("X-Fault-Before-Commit-Disconnect", "1")
	req.GetBody = nil
	resp, err := client.Do(req)
	if err == nil {
		_ = resp.Body.Close()
		t.Fatal("expected a transport error from the hijacked/closed connection")
	}

	rec, ok := e.store.Lookup("k-disco")
	if !ok || rec.Status != store.Pending {
		t.Fatalf("expected pending lease left behind, ok=%v rec=%+v", ok, rec)
	}
	if len(e.store.Ledger()) != 0 {
		t.Fatal("disconnected attempt must not commit a local effect")
	}
}

func TestAdminLedgerAndStats(t *testing.T) {
	e := newTestEnv(t)
	e.deposit(t, "k", `{"account":"zoe","amount":123}`, nil)

	resp, err := http.Get(e.biz.URL + "/admin/ledger")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var led struct {
		Effects []map[string]any `json:"effects"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&led); err != nil {
		t.Fatal(err)
	}
	if len(led.Effects) != 1 {
		t.Fatalf("effects=%d want 1", len(led.Effects))
	}

	resp2, err := http.Get(e.biz.URL + "/admin/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	b, _ := io.ReadAll(resp2.Body)
	if !bytes.Contains(b, []byte(`"committed_effects":1`)) {
		t.Fatalf("stats missing effects: %s", b)
	}
}

func TestClockAdvanceEndpoint(t *testing.T) {
	e := newTestEnv(t)
	before := e.clk.Now()
	body := bytes.NewBufferString(`{"ms":2500}`)
	resp, err := http.Post(e.biz.URL+"/admin/clock/advance", "application/json", body)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	if e.clk.Now().Sub(before) != 2500*time.Millisecond {
		t.Fatalf("clock did not advance: before=%v now=%v", before, e.clk.Now())
	}
}

// 确保处理中重复请求的等待是有界的：超过 waitMax（800ms）后明确回 202，
// 而不是被无限挂起；首个请求提交后再用同键请求即可重放。
func TestInProgressWaitIsBoundedAndThenReplays(t *testing.T) {
	e := newTestEnv(t)
	// 首个请求在提交前停留 5s（后台发起，忽略其最终结果）。
	go func() {
		req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits",
			&oneShotReader{r: bytes.NewBufferString(`{"account":"x","amount":1}`)})
		req.Header.Set("Idempotency-Key", "k-long")
		req.Header.Set("X-Fault-Delay-Before-Commit-Ms", "5000")
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			_ = resp.Body.Close()
		}
	}()
	time.Sleep(150 * time.Millisecond)

	client := &http.Client{Timeout: 5 * time.Second}
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits",
		bytes.NewBufferString(`{"account":"x","amount":1}`))
	req.Header.Set("Idempotency-Key", "k-long")
	start := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	elapsed := time.Since(start)
	// 等待必须有界：约 waitMax(800ms) 后回 202，允许一定调度余量。
	if elapsed > 2*time.Second {
		t.Fatalf("duplicate request was not bounded: %s", elapsed)
	}
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status=%d want 202 while still processing", resp.StatusCode)
	}
	if resp.Header.Get("Retry-After") == "" {
		t.Fatal("202 must carry Retry-After guidance")
	}

	// 等首个请求提交后，同键请求应重放同一结果。
	deadline := time.Now().Add(6 * time.Second)
	for time.Now().Before(deadline) {
		r := e.deposit(t, "k-long", `{"account":"x","amount":1}`, nil)
		if r.StatusCode == http.StatusOK && r.Header.Get("Idempotency-Status") == "replayed" {
			return // 成功拿到明确的重放结果
		}
		_ = r.Body.Close()
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatal("duplicate did not converge to a replayed result")
}
