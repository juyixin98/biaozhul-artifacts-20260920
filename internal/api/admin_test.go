package api

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"idempotentsave/internal/auditclient"
	"idempotentsave/internal/clock"
	"idempotentsave/internal/service"
	"idempotentsave/internal/store"
)

// nonHijackWriter 只实现 http.ResponseWriter，刻意不暴露 Hijacker 接口。
type nonHijackWriter struct {
	header http.Header
	code   int
	body   bytes.Buffer
}

func (w *nonHijackWriter) Header() http.Header {
	if w.header == nil {
		w.header = http.Header{}
	}
	return w.header
}
func (w *nonHijackWriter) Write(b []byte) (int, error) {
	if w.code == 0 {
		w.code = http.StatusOK
	}
	return w.body.Write(b)
}
func (w *nonHijackWriter) WriteHeader(code int) { w.code = code }

// timeNowPlus 返回一个在 d 秒后到期的判定函数，避免测试忙等死循环。
func timeNowPlus(d time.Duration) func() bool {
	deadline := time.Now().Add(d * time.Second)
	return func() bool { return time.Now().After(deadline) }
}

func get(t *testing.T, url string) *http.Response {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestLookupUnknownKey404(t *testing.T) {
	e := newTestEnv(t)
	r := get(t, e.biz.URL+"/v1/idempotency/nope")
	defer r.Body.Close()
	if r.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d want 404", r.StatusCode)
	}
}

func TestLookupShowsPendingThenCompleted(t *testing.T) {
	e := newTestEnv(t)
	// 后台发起一个慢请求制造 pending
	go func() {
		req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits",
			bytes.NewBufferString(`{"account":"p","amount":1}`))
		req.Header.Set("Idempotency-Key", "k-view")
		req.Header.Set("X-Fault-Delay-Before-Commit-Ms", "500")
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			_ = resp.Body.Close()
		}
	}()

	// 等待 pending 出现
	deadline := timeNowPlus(2)
	for {
		r := get(t, e.biz.URL+"/v1/idempotency/k-view")
		b, _ := io.ReadAll(r.Body)
		r.Body.Close()
		if r.StatusCode == http.StatusOK && bytes.Contains(b, []byte(`"status":"pending"`)) {
			if !bytes.Contains(b, []byte(`"expires_at"`)) {
				t.Fatalf("pending view must include expires_at: %s", b)
			}
			break
		}
		if deadline() {
			t.Fatalf("never observed pending: %s", b)
		}
	}

	// 等它完成后应可见 effect 与 response
	deadline2 := timeNowPlus(3)
	for {
		r := get(t, e.biz.URL+"/v1/idempotency/k-view")
		b, _ := io.ReadAll(r.Body)
		r.Body.Close()
		if r.StatusCode == http.StatusOK && bytes.Contains(b, []byte(`"status":"completed"`)) {
			if !bytes.Contains(b, []byte(`"effect"`)) || !bytes.Contains(b, []byte(`"response"`)) {
				t.Fatalf("completed view missing fields: %s", b)
			}
			return
		}
		if deadline2() {
			t.Fatalf("never observed completed: %s", b)
		}
	}
}

func TestMalformedJSONRejectedAndReplayed(t *testing.T) {
	e := newTestEnv(t)
	r1 := e.deposit(t, "k-bad", `{"account":`, nil)
	if r1.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422 body=%s", r1.StatusCode, bodyString(t, r1))
	}
	r2 := e.deposit(t, "k-bad", `{"account":`, nil)
	if r2.StatusCode != http.StatusUnprocessableEntity ||
		r2.Header.Get("Idempotency-Status") != "replayed" {
		t.Fatalf("malformed JSON result must be replayed: %d %q",
			r2.StatusCode, r2.Header.Get("Idempotency-Status"))
	}
}

func TestMissingAccountValidation(t *testing.T) {
	e := newTestEnv(t)
	r := e.deposit(t, "k-m", `{"amount":10}`, nil)
	if r.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422", r.StatusCode)
	}
}

func TestWrongMethod(t *testing.T) {
	e := newTestEnv(t)
	r := get(t, e.biz.URL+"/v1/deposits")
	defer r.Body.Close()
	if r.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d want 405", r.StatusCode)
	}
}

func TestEmptyBodyRejected(t *testing.T) {
	e := newTestEnv(t)
	r := e.deposit(t, "k-empty", "", nil)
	if r.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422", r.StatusCode)
	}
}

func TestAdminResetClearsEverything(t *testing.T) {
	e := newTestEnv(t)
	e.deposit(t, "k", `{"account":"r","amount":5}`, nil)
	if e.audit.Calls() != 1 {
		t.Fatalf("audit calls=%d want 1", e.audit.Calls())
	}

	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/admin/reset", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("reset status=%d", resp.StatusCode)
	}
	if e.store.Stats().Ledger != 0 || e.store.Stats().Keys != 0 {
		t.Fatalf("store not reset: %+v", e.store.Stats())
	}
	if e.audit.Calls() != 0 || e.audit.EventCount() != 0 {
		t.Fatalf("audit not reset: calls=%d events=%d", e.audit.Calls(), e.audit.EventCount())
	}
}

func TestAdminResetWrongMethod(t *testing.T) {
	e := newTestEnv(t)
	r := get(t, e.biz.URL+"/admin/reset")
	defer r.Body.Close()
	if r.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d want 405", r.StatusCode)
	}
}

func TestAdminLedgerDeleteResetsStore(t *testing.T) {
	e := newTestEnv(t)
	e.deposit(t, "k", `{"account":"z","amount":8}`, nil)
	req, _ := http.NewRequest(http.MethodDelete, e.biz.URL+"/admin/ledger", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("DELETE status=%d", resp.StatusCode)
	}
	if e.store.Stats().Ledger != 0 {
		t.Fatal("ledger not cleared")
	}
}

func TestClockAdvanceRejectsBadBody(t *testing.T) {
	e := newTestEnv(t)
	resp, err := http.Post(e.biz.URL+"/admin/clock/advance", "application/json",
		bytes.NewBufferString("not-json"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", resp.StatusCode)
	}
}

func TestAfterCommitDisconnectDurableThenReplays(t *testing.T) {
	e := newTestEnv(t)
	client := &http.Client{}
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits",
		&oneShotReader{r: bytes.NewBufferString(`{"account":"ac","amount":64}`)})
	req.Header.Set("Idempotency-Key", "k-ac")
	req.Header.Set("X-Fault-After-Commit-Disconnect", "1")
	req.GetBody = nil
	if resp, err := client.Do(req); err == nil {
		_ = resp.Body.Close()
		t.Fatal("expected transport error after commit")
	}

	// 事务已落盘：重放得到同一结果，且不再调用外部系统
	r := e.deposit(t, "k-ac", `{"account":"ac","amount":64}`, nil)
	if r.StatusCode != http.StatusOK || r.Header.Get("Idempotency-Status") != "replayed" {
		t.Fatalf("replay status=%d replay=%q", r.StatusCode, r.Header.Get("Idempotency-Status"))
	}
	if len(e.store.Ledger()) != 1 || e.store.Balance("ac") != 64 {
		t.Fatalf("ledger=%v balance=%d", e.store.Ledger(), e.store.Balance("ac"))
	}
	if e.audit.Calls() != 1 {
		t.Fatalf("audit calls=%d want 1", e.audit.Calls())
	}
}

func TestIdempotencyLookupRequiresGet(t *testing.T) {
	e := newTestEnv(t)
	req, _ := http.NewRequest(http.MethodDelete, e.biz.URL+"/v1/idempotency/x", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d want 405", resp.StatusCode)
	}
}

func TestLookupEmptyKey(t *testing.T) {
	e := newTestEnv(t)
	r := get(t, e.biz.URL+"/v1/idempotency/")
	defer r.Body.Close()
	if r.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", r.StatusCode)
	}
}

func TestLedgerRejectsUnsupportedMethod(t *testing.T) {
	e := newTestEnv(t)
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/admin/ledger", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d want 405", resp.StatusCode)
	}
}

func TestStatsRejectsNonGet(t *testing.T) {
	e := newTestEnv(t)
	req, _ := http.NewRequest(http.MethodDelete, e.biz.URL+"/admin/stats", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d want 405", resp.StatusCode)
	}
}

func TestClockAdvanceRejectsWrongMethod(t *testing.T) {
	e := newTestEnv(t)
	r := get(t, e.biz.URL+"/admin/clock/advance")
	defer r.Body.Close()
	if r.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d want 405", r.StatusCode)
	}
}

// abandonConnection 在 ResponseWriter 不支持 Hijack 时必须优雅降级而非 panic。
func TestAbandonConnectionFallbackWithoutHijacker(t *testing.T) {
	s := &Server{cfg: Config{}}
	rec := &nonHijackWriter{header: http.Header{}}
	s.abandonConnection(rec, "unit-test fallback")
	if rec.code != http.StatusServiceUnavailable {
		t.Fatalf("fallback status=%d want 503", rec.code)
	}
}

// 真实时钟模式下拨钟端点必须明确拒绝。
func TestClockAdvanceRejectedWithRealClock(t *testing.T) {
	st, err := store.Open(store.Options{Dir: t.TempDir(), PendingTTL: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	// 审计地址不可达也无妨：本测试只访问拨钟端点。
	svc := service.New(st, auditclient.New("http://127.0.0.1:0"), clock.Real{})
	srv := New(Config{
		Store: st, Service: svc, Clock: clock.Real{},
		WaitMax: time.Second, RealClock: true,
	})
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	resp, err := http.Post(ts.URL+"/admin/clock/advance", "application/json",
		bytes.NewBufferString(`{"ms":10}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 (real clock cannot be advanced)", resp.StatusCode)
	}
}

func TestOversizedBodyRejected(t *testing.T) {
	e := newTestEnv(t)
	big := bytes.Repeat([]byte("a"), (1<<20)+10)
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/v1/deposits",
		&oneShotReader{r: bytes.NewReader(big)})
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", "k-big")
	req.GetBody = nil
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status=%d want 413", resp.StatusCode)
	}
}

func TestInvalidFaultDelayHeaderIgnored(t *testing.T) {
	e := newTestEnv(t)
	r := e.deposit(t, "k-delay", `{"account":"a","amount":1}`,
		map[string]string{"X-Fault-Delay-Before-Commit-Ms": "not-a-number"})
	if r.StatusCode != http.StatusOK {
		t.Fatalf("invalid delay header must be ignored, status=%d", r.StatusCode)
	}
}

func TestStatsEndpoint(t *testing.T) {
	e := newTestEnv(t)
	e.deposit(t, "k", `{"account":"s","amount":11}`, nil)
	r := get(t, e.biz.URL+"/admin/stats")
	defer r.Body.Close()
	b, _ := io.ReadAll(r.Body)
	if r.StatusCode != http.StatusOK || !bytes.Contains(b, []byte(`"audit_calls":1`)) {
		t.Fatalf("unexpected stats: %d %s", r.StatusCode, b)
	}
}

// 占位 TTL 过期被新尝试接管后，旧执行者迟到的提交必须被拒（lease_lost），
// 本地副作用仍只有新尝试那一笔。
func TestStaleCommitAfterLeaseReclaimed(t *testing.T) {
	e := newTestEnv(t)
	body := `{"account":"ll","amount":15}`
	// A 占位后停留 800ms；B 回收后停留 1500ms（保证 A 醒来时 B 仍持占位）。
	hdrA := map[string]string{"X-Fault-Delay-Before-Commit-Ms": "800"}
	hdrB := map[string]string{"X-Fault-Delay-Before-Commit-Ms": "1500"}

	var stale *http.Response
	doneA := make(chan struct{})
	go func() { // A：占位后停留，其租约在期间被回收
		defer close(doneA)
		stale = e.deposit(t, "k-ll", body, hdrA)
	}()

	// 等 A 占位，然后把假时钟拨过 1 分钟 TTL
	time.Sleep(150 * time.Millisecond)
	e.clk.Advance(61 * time.Second)

	doneB := make(chan struct{})
	var reclaimed *http.Response
	go func() { // B：以新代次回收，占位保持到 A 醒来之后，再提交
		defer close(doneB)
		reclaimed = e.deposit(t, "k-ll", body, hdrB)
	}()

	<-doneA
	if stale.StatusCode != http.StatusConflict {
		t.Fatalf("stale A commit status=%d want 409", stale.StatusCode)
	}
	sb := bodyString(t, stale)
	if !bytes.Contains([]byte(sb), []byte("lease_lost")) {
		t.Fatalf("stale commit should report lease_lost: %s", sb)
	}
	if len(e.store.Ledger()) != 0 {
		t.Fatal("no effect may exist while B is still processing")
	}

	<-doneB
	if reclaimed.StatusCode != http.StatusOK {
		t.Fatalf("reclaiming B status=%d want 200", reclaimed.StatusCode)
	}
	if len(e.store.Ledger()) != 1 || e.store.Balance("ll") != 15 {
		t.Fatalf("ledger=%v balance=%d, want one effect of 15", e.store.Ledger(), e.store.Balance("ll"))
	}
}

// 外部假审计系统不可达时，观测/复位端点必须降级而不是拖垮业务。
func TestAuditUnreachableDegradesGracefully(t *testing.T) {
	e := newTestEnv(t)
	e.deposit(t, "k", `{"account":"g","amount":3}`, nil)
	e.closeAudit() // 关闭审计 httptest 服务

	r := get(t, e.biz.URL+"/admin/stats")
	b, _ := io.ReadAll(r.Body)
	r.Body.Close()
	if r.StatusCode != http.StatusOK || !bytes.Contains(b, []byte(`"audit_calls":-1`)) {
		t.Fatalf("stats should degrade with audit_calls=-1: %d %s", r.StatusCode, b)
	}

	// reset 在审计不可达时仍应成功（尽力复位外部）
	req, _ := http.NewRequest(http.MethodPost, e.biz.URL+"/admin/reset", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("reset status=%d want 200 even if audit is down", resp.StatusCode)
	}
}
