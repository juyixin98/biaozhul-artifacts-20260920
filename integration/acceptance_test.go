// Package integration_test 通过真实 HTTP（httptest）端到端验证验收场景：
// 暂停旧持有者 → 租约过期 → 新持有者写入 → 旧写入被拒绝 → 重启后令牌不回退。
package integration_test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"fencingdemo/clock"
	"fencingdemo/locksvc"
	"fencingdemo/resourcesvc"
)

// stack 是本地双组件模拟：两个独立 HTTP 服务 + 共享的可注入手动时钟。
type stack struct {
	clk     *clock.Fake
	lockSrv *httptest.Server
	resSrv  *httptest.Server
}

func start(t *testing.T, dir string, clk *clock.Fake) *stack {
	t.Helper()
	lockSvc, err := locksvc.NewService(clk, dir)
	if err != nil {
		t.Fatal(err)
	}
	resSvc, err := resourcesvc.NewService(dir)
	if err != nil {
		t.Fatal(err)
	}
	return &stack{
		clk:     clk,
		lockSrv: httptest.NewServer(lockSvc.HTTPHandler()),
		resSrv:  httptest.NewServer(resSvc.HTTPHandler()),
	}
}

func (s *stack) close() {
	s.lockSrv.Close()
	s.resSrv.Close()
}

func (s *stack) acquire(t *testing.T, holder string, ttl time.Duration) (status int, body map[string]any) {
	t.Helper()
	body = map[string]any{}
	resp := postJSON(t, s.lockSrv.URL+"/v1/locks/doc/acquire", map[string]any{
		"holder": holder, "ttl_ms": ttl.Milliseconds(),
	})
	defer resp.Body.Close()
	readJSON(t, resp.Body, &body)
	return resp.StatusCode, body
}

func (s *stack) write(t *testing.T, token uint64, value string) (status int, body map[string]any) {
	t.Helper()
	body = map[string]any{}
	req, _ := http.NewRequest(http.MethodPut, s.resSrv.URL+"/v1/resources/config",
		bytes.NewReader(mustJSON(map[string]any{"token": token, "value": value})))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	readJSON(t, resp.Body, &body)
	return resp.StatusCode, body
}

func (s *stack) counter(t *testing.T) uint64 {
	t.Helper()
	resp, err := http.Get(s.lockSrv.URL + "/v1/counter")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body struct {
		Counter uint64 `json:"counter"`
	}
	readJSON(t, resp.Body, &body)
	return body.Counter
}

func (s *stack) readResource(t *testing.T) map[string]any {
	t.Helper()
	resp, err := http.Get(s.resSrv.URL + "/v1/resources/config")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body map[string]any
	readJSON(t, resp.Body, &body)
	return body
}

// TestAcceptanceFencingScenario 复现题目要求的完整验收过程。
func TestAcceptanceFencingScenario(t *testing.T) {
	dir := t.TempDir()
	startTime := time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC)
	clk := clock.NewFake(startTime)
	st := start(t, dir, clk)

	const ttl = 10 * time.Second

	// 1) 旧持有者 A 获取锁，拿到令牌 1。
	status, aLease := st.acquire(t, "holder-A", ttl)
	if status != http.StatusOK || aLease["token"].(float64) != 1 {
		t.Fatalf("A acquire: status=%d body=%v", status, aLease)
	}
	tokenA := uint64(aLease["token"].(float64))

	// 2) 租约未过期时 B 抢锁：409。
	if status, _ := st.acquire(t, "holder-B", ttl); status != http.StatusConflict {
		t.Fatalf("B acquire before expiry: status=%d, want 409", status)
	}

	// 3) A “暂停”（不续租），推进注入时钟至租约过期之后。
	clk.Advance(ttl + time.Millisecond)

	// 4) B 获取锁成功，拿到更大的围栏令牌 2。
	status, bLease := st.acquire(t, "holder-B", ttl)
	if status != http.StatusOK || bLease["token"].(float64) != 2 {
		t.Fatalf("B acquire after expiry: status=%d body=%v", status, bLease)
	}
	tokenB := uint64(bLease["token"].(float64))

	// 5) 新持有者 B 先写：接受。
	if status, body := st.write(t, tokenB, "value-from-B"); status != http.StatusOK {
		t.Fatalf("B write: status=%d body=%v, want 200", status, body)
	}

	// 6) A 恢复，用旧令牌 1 迟到写入：必须被拒绝。
	status, rejected := st.write(t, tokenA, "value-from-A-late")
	if status != http.StatusConflict {
		t.Fatalf("A late write: status=%d, want 409", status)
	}
	if rejected["seen"].(float64) != float64(tokenB) {
		t.Fatalf("reject body seen = %v, want %d", rejected["seen"], tokenB)
	}
	t.Logf("旧持有者写入被拒绝: %v", rejected)

	// 7) 资源内容仍是 B 的。
	res := st.readResource(t)
	if res["value"] != "value-from-B" || uint64(res["token"].(float64)) != tokenB {
		t.Fatalf("resource = %v, want B's value/token", res)
	}
	st.close()

	// 8) 模拟进程重启：同一状态目录、全新服务实例（时钟继续前进）。
	clk.Advance(time.Second)
	st2 := start(t, dir, clk)
	defer st2.close()

	if c := st2.counter(t); c != 2 {
		t.Fatalf("counter after restart = %d, want 2 (不回退)", c)
	}
	// 重启后 A 的旧令牌写入依然被拒绝（已见令牌也持久化）。
	if status, _ := st2.write(t, tokenA, "A-after-restart"); status != http.StatusConflict {
		t.Fatalf("A stale write after restart: status=%d, want 409", status)
	}
	// B 的租约尚未过期时，A 仍抢不到锁。
	if status, _ := st2.acquire(t, "holder-A", ttl); status != http.StatusConflict {
		t.Fatalf("A acquire while B active after restart: status=%d, want 409", status)
	}

	// 9) B 租约过期，C 获取锁得到令牌 3——计数在重启基础上继续递增。
	clk.Advance(ttl + time.Millisecond)
	status, cLease := st2.acquire(t, "holder-C", ttl)
	if status != http.StatusOK || cLease["token"].(float64) != 3 {
		t.Fatalf("C acquire after restart+expiry: status=%d body=%v, want token 3", status, cLease)
	}
	// C 写入成功；此时 B 的令牌 2 已成旧令牌。
	if status, body := st2.write(t, 3, "value-from-C"); status != http.StatusOK {
		t.Fatalf("C write: status=%d body=%v", status, body)
	}
	if status, _ := st2.write(t, tokenB, "B-late"); status != http.StatusConflict {
		t.Fatalf("B late write after C: status=%d, want 409", status)
	}
}

func postJSON(t *testing.T, url string, v any) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", bytes.NewReader(mustJSON(v)))
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func readJSON(t *testing.T, r io.Reader, v any) {
	t.Helper()
	if err := json.NewDecoder(r).Decode(v); err != nil {
		t.Fatalf("decode response: %v", err)
	}
}
