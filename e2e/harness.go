package e2e

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"inbox/internal/testsupport"
)

// server is a running inboxd subprocess bound to an isolated test schema.
type server struct {
	t       *testing.T
	cmd     *exec.Cmd
	baseURL string
	addr    string
	dsn     string
	schema  string
	logFile *os.File
	bin     string
	stopped bool
}

// buildServer compiles inboxd once per test invocation into a temp binary.
func buildServer(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "inboxd")
	cmd := exec.Command("go", "build", "-o", bin, "./cmd/inboxd")
	cmd.Dir = repoRoot(t)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("build inboxd: %v", err)
	}
	return bin
}

// repoRoot locates the module root from the test working directory.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found")
		}
		dir = parent
	}
}

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

// startServer launches inboxd against a fresh isolated schema. crashSpec
// is passed through INBOX_CRASH (empty disables it). The background worker
// is effectively disabled via a long interval so tests drive /process.
func startServer(t *testing.T, bin, crashSpec string) *server {
	t.Helper()
	schema := testsupport.NewSchemaOnly(t)
	return startOnSchema(t, bin, schema, crashSpec)
}

// startOnSchema relaunches inboxd against an existing schema (used after a
// real process crash to test durable recovery and exactly-once restart).
func startOnSchema(t *testing.T, bin, schema, crashSpec string) *server {
	t.Helper()
	addr := freePort(t)
	logPath := filepath.Join(t.TempDir(), "inboxd.log")
	lf, err := os.Create(logPath)
	if err != nil {
		t.Fatal(err)
	}
	dsn := testsupport.SchemaDSN(schema)

	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(),
		"INBOX_DB="+dsn,
		"INBOX_ADDR="+addr,
		"INBOX_INTERVAL=1h",
		"INBOX_WORKER=off",
	)
	if crashSpec != "" {
		cmd.Env = append(cmd.Env, "INBOX_CRASH="+crashSpec)
	}
	cmd.Stdout = lf
	cmd.Stderr = lf
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	s := &server{t: t, cmd: cmd, baseURL: "http://" + addr, addr: addr, dsn: dsn, schema: schema, logFile: lf, bin: bin}
	s.waitReady(10 * time.Second)
	t.Cleanup(s.stop)
	return s
}

// restart relaunches a stopped server against the same schema and port
// pool, optionally with a new crash spec.
func (s *server) restart(crashSpec string) *server {
	return startOnSchema(s.t, s.bin, s.schema, crashSpec)
}

func (s *server) waitReady(timeout time.Duration) {
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: time.Second}
	for time.Now().Before(deadline) {
		resp, err := client.Get(s.baseURL + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == 200 {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	out, _ := os.ReadFile(s.logFile.Name())
	s.t.Fatalf("server did not become ready; log:\n%s", string(out))
}

func (s *server) stop() {
	if s.stopped || s.cmd == nil || s.cmd.Process == nil {
		return
	}
	s.stopped = true
	_ = s.cmd.Process.Signal(os.Interrupt)
	done := make(chan struct{})
	go func() { _ = s.cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = s.cmd.Process.Kill()
		<-done
	}
	_ = s.logFile.Close()
	s.cmd = nil
}

// exitCode waits for the process and returns its exit code, marking the
// server stopped (the process is gone).
func (s *server) exitCode() int {
	err := s.cmd.Wait()
	s.stopped = true
	_ = s.logFile.Close()
	if err == nil {
		return 0
	}
	if ee, ok := err.(*exec.ExitError); ok {
		return ee.ExitCode()
	}
	return -1
}

// crashAndExpectExit triggers /process and waits for the crash exit code.
func (s *server) crashAndExpectExit(wantCode int) {
	// Fire the delivery attempt; the process hard-exits inside the
	// transaction. Use a client that tolerates the dead connection.
	go func() {
		_, _ = http.Post(s.baseURL+"/v1/process", "application/json", bytes.NewReader([]byte("{}")))
	}()
	done := make(chan int, 1)
	go func() { done <- s.exitCode() }()
	select {
	case code := <-done:
		if code != wantCode {
			s.t.Fatalf("crash exit code=%d, want %d", code, wantCode)
		}
	case <-time.After(10 * time.Second):
		s.t.Fatal("server did not crash within 10s")
	}
	s.cmd = nil // process is gone
}

func (s *server) post(path string, body any) (int, map[string]any) {
	buf, _ := json.Marshal(body)
	resp, err := http.Post(s.baseURL+path, "application/json", bytes.NewReader(buf))
	if err != nil {
		s.t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	return resp.StatusCode, decodeMap(s.t, resp.Body)
}

func (s *server) get(path string) (int, []map[string]any) {
	resp, err := http.Get(s.baseURL + path)
	if err != nil {
		s.t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out []map[string]any
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

func decodeMap(t *testing.T, r io.Reader) map[string]any {
	t.Helper()
	var m map[string]any
	raw, _ := io.ReadAll(r)
	_ = json.Unmarshal(raw, &m)
	if m == nil {
		m = map[string]any{"raw": string(raw)}
	}
	return m
}
