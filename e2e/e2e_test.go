// Package e2e builds the real server binary and drives it over HTTP:
// ingest including overflow, graceful shutdown, restart recovery.
package e2e

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"
	"time"
)

var serverBin string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "cb-e2e-")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	serverBin = filepath.Join(dir, "server")
	_, thisFile, _, _ := runtime.Caller(0)
	moduleRoot := filepath.Dir(thisFile) // e2e/ directory; module root is its parent
	build := exec.Command("go", "build", "-o", serverBin, "./cmd/server")
	build.Dir = filepath.Dir(moduleRoot)
	build.Stdout, build.Stderr = os.Stdout, os.Stderr
	if err := build.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "build server:", err)
		os.Exit(1)
	}
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

type serverProc struct {
	cmd     *exec.Cmd
	baseURL string
	dataDir string
}

func startServer(t *testing.T, dataDir string, args ...string) *serverProc {
	t.Helper()
	addr := fmt.Sprintf("127.0.0.1:%d", 18080+(os.Getpid()%2000))
	if v := os.Getenv("CB_E2E_PORT"); v != "" {
		addr = "127.0.0.1:" + v
	}
	fullArgs := append([]string{"-addr", addr, "-data-dir", dataDir,
		"-max-series", "5", "-max-metrics", "5", "-flush-interval", "1s"}, args...)
	cmd := exec.Command(serverBin, fullArgs...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start server: %v", err)
	}
	sp := &serverProc{cmd: cmd, baseURL: "http://" + addr, dataDir: dataDir}
	sp.waitReady(t)
	return sp
}

func (sp *serverProc) waitReady(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		resp, err := http.Get(sp.baseURL + "/healthz")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
			lastErr = fmt.Errorf("status %d: %s", resp.StatusCode, body)
		} else {
			lastErr = err
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("server not ready: %v", lastErr)
}

func (sp *serverProc) stopGraceful(t *testing.T) {
	t.Helper()
	if err := sp.cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("sigterm: %v", err)
	}
	done := make(chan error, 1)
	go func() { done <- sp.cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			// exit status 0 expected
			t.Fatalf("server exited: %v", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("server did not shut down within 8s")
	}
}

func (sp *serverProc) kill(t *testing.T) {
	t.Helper()
	_ = sp.cmd.Process.Kill()
	_ = sp.cmd.Wait()
}

func postJSON(t *testing.T, url string, payload any) map[string]any {
	t.Helper()
	raw, _ := json.Marshal(payload)
	resp, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST %s -> %d: %s", url, resp.StatusCode, body)
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func getJSON(t *testing.T, url string) map[string]any {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s -> %d: %s", url, resp.StatusCode, body)
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// TestIngestRestartRecover: send traffic (normal + attack + rejection),
// shut down gracefully (final flush), restart, and assert exact recovery.
func TestIngestRestartRecover(t *testing.T) {
	dataDir := t.TempDir()

	// --- run 1 ---
	sp := startServer(t, dataDir)

	// 5 stable combos fill the budget; then 20 unique attack combos overflow.
	samples := make([]map[string]any, 0, 25)
	for i := 0; i < 5; i++ {
		samples = append(samples, map[string]any{
			"metric": "http_requests",
			"labels": map[string]string{"path": fmt.Sprintf("/p%d", i)},
			"value":  1,
		})
	}
	for i := 0; i < 20; i++ {
		samples = append(samples, map[string]any{
			"metric": "http_requests",
			"labels": map[string]string{"request_id": fmt.Sprintf("attacker-%d", i)},
			"value":  2,
		})
	}
	out := postJSON(t, sp.baseURL+"/api/v1/ingest", map[string]any{"samples": samples})
	if out["accepted"].(float64) != 25 || out["overflow"].(float64) != 20 {
		t.Fatalf("ingest summary: %v", out)
	}

	// A rejected sample to persist the rejected counter too.
	postJSON(t, sp.baseURL+"/api/v1/ingest", map[string]any{
		"samples": []map[string]any{{"metric": "", "value": 1}},
	})

	statsBefore := getJSON(t, sp.baseURL+"/api/v1/stats")
	metricBefore := getJSON(t, sp.baseURL+"/api/v1/metrics/http_requests?series=1")

	// Force a flush, then also rely on graceful final flush.
	postJSON(t, sp.baseURL+"/debug/flush", map[string]any{})
	sp.stopGraceful(t)

	// Snapshot file exists and is non-empty.
	if fi, err := os.Stat(filepath.Join(dataDir, "snapshot.json")); err != nil || fi.Size() == 0 {
		t.Fatalf("snapshot not persisted: %v", err)
	}

	// --- run 2: restart from the same data dir ---
	sp2 := startServer(t, dataDir)
	defer sp2.kill(t)

	statsAfter := getJSON(t, sp2.baseURL+"/api/v1/stats")
	metricAfter := getJSON(t, sp2.baseURL+"/api/v1/metrics/http_requests?series=1")

	for _, key := range []string{
		"samples_received", "samples_accepted", "samples_rejected",
		"overflow_samples", "normal_samples", "series_total",
		"overflow_series_total", "metric_names",
	} {
		if statsBefore[key] != statsAfter[key] {
			t.Fatalf("stats %s: before=%v after=%v", key, statsBefore[key], statsAfter[key])
		}
	}
	if metricBefore["count"] != metricAfter["count"] ||
		metricBefore["normal_series"] != metricAfter["normal_series"] ||
		metricBefore["overflow_count"] != metricAfter["overflow_count"] {
		t.Fatalf("metric view differs:\nbefore=%v\nafter =%v", metricBefore, metricAfter)
	}
	if len(metricBefore["series"].([]any)) != len(metricAfter["series"].([]any)) {
		t.Fatal("series count differs after restart")
	}

	// Post-restart writes: stable combo still incremented in place; new
	// unique combos keep going to overflow.
	out = postJSON(t, sp2.baseURL+"/api/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "http_requests", "labels": map[string]string{"path": "/p0"}, "value": 1},
			{"metric": "http_requests", "labels": map[string]string{"request_id": "fresh-attack"}, "value": 1},
		},
	})
	if out["overflow"].(float64) != 1 {
		t.Fatalf("post-restart overflow: %v", out)
	}
}

// TestHardKillRecovery relies only on periodic flush: kill -9, then restart.
func TestHardKillRecovery(t *testing.T) {
	dataDir := t.TempDir()
	sp := startServer(t, dataDir)

	postJSON(t, sp.baseURL+"/api/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "m", "labels": map[string]string{"k": "v0"}, "value": 1},
			{"metric": "m", "labels": map[string]string{"k": "v1"}, "value": 1},
		},
	})
	// Wait past the 1s periodic flush interval.
	time.Sleep(2500 * time.Millisecond)
	sp.kill(t)

	sp2 := startServer(t, dataDir)
	defer sp2.kill(t)
	stats := getJSON(t, sp2.baseURL+"/api/v1/stats")
	if accepted := stats["samples_accepted"].(float64); accepted != 2 {
		t.Fatalf("after hard kill, accepted=%v want 2", accepted)
	}
}
