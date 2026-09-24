// Package e2etest contains black-box tests that drive the lock and resource
// services only through their HTTP APIs, exactly as external clients would.
package e2etest

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"fencingdemo/clock"
	"fencingdemo/internal/lock"
	"fencingdemo/internal/resource"
)

// startLock creates a fresh lock HTTP server backed by path. Calling this
// again with the same path simulates a lock-service restart.
func startLock(t *testing.T, clk clock.Clock, path string, ttl time.Duration) string {
	t.Helper()
	store, err := lock.NewStore(clk, path, ttl)
	if err != nil {
		t.Fatalf("open lock store: %v", err)
	}
	return startHTTP(t, lock.NewServer(store).Handler())
}

func startResource(t *testing.T, path string) string {
	t.Helper()
	store, err := resource.NewStore(path)
	if err != nil {
		t.Fatalf("open resource store: %v", err)
	}
	return startHTTP(t, resource.NewServer(store).Handler())
}

type httpServer interface {
	Close()
	URL() string
}

func startHTTP(t *testing.T, h http.Handler) string {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return srv.URL
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal request: %v", err)
		}
		rdr = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(bytes.TrimSpace(data)) > 0 {
		_ = json.Unmarshal(data, &out)
	}
	return resp.StatusCode, out
}

func mustToken(t *testing.T, m map[string]any) int64 {
	t.Helper()
	v, ok := m["fencing_token"].(float64)
	if !ok {
		t.Fatalf("response has no fencing_token: %v", m)
	}
	return int64(v)
}

// TestFencingAcceptanceScenario reproduces the required acceptance test:
//
//  1. old holder A gets token 1 and writes successfully;
//  2. A is "paused" until its lease expires;
//  3. new holder B gets a larger token and writes;
//  4. A resumes and retries its write with the stale token -> rejected;
//  5. both services are restarted: tokens keep increasing and the stale write
//     is still rejected.
func TestFencingAcceptanceScenario(t *testing.T) {
	const ttl = 5 * time.Second
	clk := clock.NewFake(time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC))
	dir := t.TempDir()
	lockPath := filepath.Join(dir, "lock.json")
	resPath := filepath.Join(dir, "resource.json")

	lockURL := startLock(t, clk, lockPath, ttl)
	resURL := startResource(t, resPath)

	post := func(path string, body any) (int, map[string]any) {
		return doJSON(t, http.MethodPost, path, body)
	}

	// 1. A acquires the lock and receives fencing token 1.
	st, body := post(lockURL+"/lock/acquire", map[string]string{"lease_id": "A"})
	if st != http.StatusOK {
		t.Fatalf("A acquire: status %d body %v", st, body)
	}
	tokenA := mustToken(t, body)
	if tokenA != 1 {
		t.Fatalf("A token = %d, want 1", tokenA)
	}
	t.Logf("A acquired lock with fencing token %d", tokenA)

	// A's write is accepted.
	st, body = post(resURL+"/resource/write", map[string]any{
		"fencing_token": tokenA, "lease_id": "A", "value": "write-by-A",
	})
	if st != http.StatusOK || body["accepted"] != true {
		t.Fatalf("A write: status %d body %v", st, body)
	}
	t.Log("A write accepted")

	// 2. A is paused (it never renews). Time advances past the lease TTL.
	clk.Advance(ttl + time.Second)
	t.Logf("clock advanced past TTL (%s): A's lease is expired", ttl)

	// A's post-expiry renew/release must fail.
	if st, body = post(lockURL+"/lock/renew", map[string]string{"lease_id": "A"}); st != http.StatusGone {
		t.Fatalf("A renew after expiry: status %d body %v, want 410", st, body)
	}
	if st, body = post(lockURL+"/lock/release", map[string]string{"lease_id": "A"}); st != http.StatusGone {
		t.Fatalf("A release after expiry: status %d body %v, want 410", st, body)
	}

	// 3. B acquires and gets a strictly larger token.
	st, body = post(lockURL+"/lock/acquire", map[string]string{"lease_id": "B"})
	if st != http.StatusOK {
		t.Fatalf("B acquire: status %d body %v", st, body)
	}
	tokenB := mustToken(t, body)
	if tokenB <= tokenA {
		t.Fatalf("B token %d not greater than A token %d", tokenB, tokenA)
	}
	t.Logf("B acquired lock after A's expiry with fencing token %d", tokenB)

	// B's write wins.
	st, body = post(resURL+"/resource/write", map[string]any{
		"fencing_token": tokenB, "lease_id": "B", "value": "write-by-B",
	})
	if st != http.StatusOK || body["accepted"] != true {
		t.Fatalf("B write: status %d body %v", st, body)
	}
	t.Log("B write accepted")

	// 4. A resumes and retries its write with the OLD token. The resource
	//    must reject it without changing state.
	st, body = post(resURL+"/resource/write", map[string]any{
		"fencing_token": tokenA, "lease_id": "A", "value": "stale-write-by-A",
	})
	if st != http.StatusConflict || body["accepted"] != false {
		t.Fatalf("stale A write: status %d body %v, want 409 accepted=false", st, body)
	}
	if hw, _ := body["high_water_mark"].(float64); int64(hw) != tokenB {
		t.Fatalf("high-water mark = %v, want %d", body["high_water_mark"], tokenB)
	}
	t.Logf("A's resumed write with stale token %d REJECTED (high-water mark %d)",
		tokenA, tokenB)

	// The stored value must still be B's.
	st, body = doJSON(t, http.MethodGet, resURL+"/resource/read", nil)
	if st != http.StatusOK || body["value"] != "write-by-B" {
		t.Fatalf("read after stale write: status %d body %v", st, body)
	}

	// 5a. Restart the lock service from the same file. Counter must survive.
	lockURL2 := startLock(t, clk, lockPath, ttl)
	st, body = doJSON(t, http.MethodGet, lockURL2+"/lock/status", nil)
	if st != http.StatusOK {
		t.Fatalf("lock status after restart: %d %v", st, body)
	}
	next, _ := body["next_token"].(float64)
	if int64(next) != tokenB+1 {
		t.Fatalf("next_token after lock restart = %v, want %d", next, tokenB+1)
	}
	t.Logf("lock restarted; next token to be issued is %d (no rollback)", int64(next))

	// Let B's lease expire; C acquires after the restart and gets token 3.
	clk.Advance(ttl + time.Second)
	st, body = post(lockURL2+"/lock/acquire", map[string]string{"lease_id": "C"})
	if st != http.StatusOK {
		t.Fatalf("C acquire after restart: %d %v", st, body)
	}
	tokenC := mustToken(t, body)
	if tokenC != tokenB+1 {
		t.Fatalf("C token after restart = %d, want %d", tokenC, tokenB+1)
	}
	t.Logf("C acquired after restart with token %d", tokenC)

	// C writes with its fresh token, advancing the resource high-water mark.
	st, body = post(resURL+"/resource/write", map[string]any{
		"fencing_token": tokenC, "lease_id": "C", "value": "write-by-C",
	})
	if st != http.StatusOK || body["accepted"] != true {
		t.Fatalf("C write: status %d body %v", st, body)
	}

	// 5b. Restart the resource service. The high-water mark must survive, so
	//     writes carrying A's or B's older tokens are still rejected.
	resURL2 := startResource(t, resPath)
	for _, tc := range []struct {
		name  string
		token int64
	}{
		{"A", tokenA}, {"B", tokenB},
	} {
		st, body = post(resURL2+"/resource/write", map[string]any{
			"fencing_token": tc.token, "lease_id": tc.name, "value": "stale-after-restart",
		})
		if st != http.StatusConflict || body["accepted"] != false {
			t.Fatalf("%s write after resource restart: status %d body %v, want 409",
				tc.name, st, body)
		}
		t.Logf("%s's stale write (token %d) still rejected after resource restart",
			tc.name, tc.token)
	}

	// C's current token (equal to the persisted high-water mark) writes fine.
	st, body = post(resURL2+"/resource/write", map[string]any{
		"fencing_token": tokenC, "lease_id": "C", "value": "write-by-C-again",
	})
	if st != http.StatusOK || body["accepted"] != true {
		t.Fatalf("C write after restart: status %d body %v", st, body)
	}

	// Final stored value must be C's.
	st, body = doJSON(t, http.MethodGet, resURL2+"/resource/read", nil)
	if st != http.StatusOK || body["value"] != "write-by-C-again" {
		t.Fatalf("final read: status %d body %v", st, body)
	}
}

// TestConcurrentAcquiresOnlyOneWins fires many distinct clients at a free
// lock simultaneously and asserts exactly one wins (mutex correctness).
func TestConcurrentAcquiresOnlyOneWins(t *testing.T) {
	const n = 50
	dir := t.TempDir()
	lockURL := startLock(t, clock.Real{}, filepath.Join(dir, "lock.json"), 10*time.Second)

	var wg sync.WaitGroup
	statuses := make([]int, n)
	tokens := make([]int64, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			st, body := doJSON(t, http.MethodPost, lockURL+"/lock/acquire",
				map[string]string{"lease_id": fmt.Sprintf("client-%d", i)})
			statuses[i] = st
			if st == http.StatusOK {
				tokens[i] = mustToken(t, body)
			}
		}(i)
	}
	close(start)
	wg.Wait()

	winners, winnerTokens := 0, map[int64]bool{}
	for i, st := range statuses {
		if st == http.StatusOK {
			winners++
			winnerTokens[tokens[i]] = true
		} else if st != http.StatusConflict {
			t.Fatalf("client %d unexpected status %d", i, st)
		}
	}
	if winners != 1 {
		t.Fatalf("exactly one winner expected, got %d", winners)
	}
	if len(winnerTokens) != 1 {
		t.Fatalf("winner got multiple tokens: %v", winnerTokens)
	}
}

// TestWallClockEndToEnd runs the same stale-writer scenario against the real
// wall clock, proving the injected-clock design behaves correctly in
// production timing too.
func TestWallClockEndToEnd(t *testing.T) {
	const ttl = 250 * time.Millisecond
	dir := t.TempDir()
	lockURL := startLock(t, clock.Real{}, filepath.Join(dir, "lock.json"), ttl)
	resURL := startResource(t, filepath.Join(dir, "resource.json"))

	post := func(path string, body any) (int, map[string]any) {
		return doJSON(t, http.MethodPost, path, body)
	}

	st, body := post(lockURL+"/lock/acquire", map[string]string{"lease_id": "A"})
	if st != http.StatusOK {
		t.Fatalf("A acquire: %d %v", st, body)
	}
	tokenA := mustToken(t, body)

	st, body = post(resURL+"/resource/write", map[string]any{
		"fencing_token": tokenA, "value": "A",
	})
	if st != http.StatusOK {
		t.Fatalf("A write: %d", st)
	}

	time.Sleep(ttl + 200*time.Millisecond) // let A's lease expire on the wall clock

	st, body = post(lockURL+"/lock/acquire", map[string]string{"lease_id": "B"})
	if st != http.StatusOK {
		t.Fatalf("B acquire: %d %v", st, body)
	}
	tokenB := mustToken(t, body)
	if tokenB <= tokenA {
		t.Fatalf("B token %d <= A token %d", tokenB, tokenA)
	}
	st, _ = post(resURL+"/resource/write", map[string]any{"fencing_token": tokenB, "value": "B"})
	if st != http.StatusOK {
		t.Fatalf("B write: %d", st)
	}
	st, body = post(resURL+"/resource/write", map[string]any{"fencing_token": tokenA, "value": "A-late"})
	if st != http.StatusConflict || body["accepted"] != false {
		t.Fatalf("stale wall-clock write: status %d body %v, want 409", st, body)
	}
}
