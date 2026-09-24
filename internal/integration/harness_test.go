// Package integration contains process-level tests: real HTTP servers run as
// separate OS processes with real WAL files, crash points kill the process
// with os.Exit(42), and restarted nodes replay their logs.
package integration

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

var (
	binPath string
	binOnce sync.Once
	binErr  error
)

// goBin returns a usable go executable even when PATH lacks /usr/local/go/bin.
func goBin() string {
	if p, err := exec.LookPath("go"); err == nil {
		return p
	}
	if _, err := os.Stat("/usr/local/go/bin/go"); err == nil {
		return "/usr/local/go/bin/go"
	}
	return "go"
}

func buildBinary(t *testing.T) string {
	t.Helper()
	binOnce.Do(func() {
		// Process-stable directory: t.TempDir() of the first test to build
		// would be deleted when that test ends.
		dir, err := os.MkdirTemp("", "tpc-integration-bin")
		if err != nil {
			binErr = err
			return
		}
		binPath = filepath.Join(dir, "tpc")
		cmd := exec.Command(goBin(), "build", "-o", binPath, "../../cmd/tpc")
		cmd.Env = append([]string{}, os.Environ()...)
		var buildErr bytes.Buffer
		cmd.Stderr = &buildErr
		if err := cmd.Run(); err != nil {
			binErr = fmt.Errorf("%w: %s", err, buildErr.String())
		}
	})
	if binErr != nil {
		t.Fatalf("build test binary: %v", binErr)
	}
	return binPath
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().(*net.TCPAddr)
	l.Close()
	return addr.Port
}

// node is a running (or stopped) node process.
type node struct {
	t        *testing.T
	role     string // coordinator | participant
	name     string
	port     int
	datadir  string
	crash    string // TPC_CRASH value
	cmd      *exec.Cmd
	logBuf   *threadSafeBuffer
	stopped  bool
	partsArg string // coordinator only
}

type threadSafeBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *threadSafeBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}
func (b *threadSafeBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func (n *node) url() string { return fmt.Sprintf("http://127.0.0.1:%d", n.port) }

// start launches the node and waits for /health.
func (n *node) start() {
	n.t.Helper()
	bin := buildBinary(n.t)
	args := []string{n.role, "-name", n.name, "-listen", fmt.Sprintf("127.0.0.1:%d", n.port), "-datadir", n.datadir}
	if n.role == "coordinator" {
		args = append(args, "-participants", n.partsArg)
	}
	cmd := exec.Command(bin, args...)
	env := append([]string{}, os.Environ()...)
	if n.crash != "" {
		env = append(env, "TPC_CRASH="+n.crash)
	}
	cmd.Env = env
	n.logBuf = &threadSafeBuffer{}
	cmd.Stdout = n.logBuf
	cmd.Stderr = n.logBuf
	if err := cmd.Start(); err != nil {
		n.t.Fatalf("start %s: %v", n.name, err)
	}
	n.cmd = cmd
	n.waitHealthy(10 * time.Second)
}

func (n *node) waitHealthy(timeout time.Duration) {
	n.t.Helper()
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: 300 * time.Millisecond}
	for time.Now().Before(deadline) {
		resp, err := client.Get(n.url() + "/health")
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 {
				return
			}
		}
		time.Sleep(30 * time.Millisecond)
	}
	n.t.Fatalf("node %s did not become healthy\nlogs:\n%s", n.name, n.logBuf.String())
}

// alive reports whether the process still exists and is not a zombie. A
// process killed by a crash point (os.Exit) becomes a zombie until reaped and
// kill(pid, 0) still succeeds for it, so we parse /proc/<pid>/stat state.
func (n *node) alive() bool {
	if n.cmd == nil || n.cmd.Process == nil {
		return false
	}
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", n.cmd.Process.Pid))
	if err != nil {
		return false
	}
	// Fields after the comm (which may contain spaces/parens) start after
	// the last ')'; the next token is the state field.
	rest := string(data)
	if i := strings.LastIndexByte(rest, ')'); i >= 0 {
		rest = rest[i+2:]
	}
	fields := strings.Fields(rest)
	return len(fields) > 0 && fields[0] != "Z"
}

// reap collects the process once it has exited. Callers must have already
// established that the process is no longer alive (or must be prepared to
// block until it exits).
func (n *node) reap() {
	if n.cmd != nil && n.cmd.ProcessState == nil {
		_ = n.cmd.Wait()
	}
}

// kill simulates a hard crash: SIGKILL, no graceful shutdown, defers do not
// run. It then reaps the process so the next start is clean.
func (n *node) kill() {
	n.t.Helper()
	if n.alive() {
		if err := n.cmd.Process.Kill(); err != nil {
			n.t.Logf("kill %s: %v", n.name, err)
		}
	}
	n.reap()
	n.stopped = true
}

// restart re-launches the node from the same datadir. If the previous
// instance died at a crash point (or is still alive) it is killed/reaped
// first. A crash value of "" means a clean restart; state comes from the
// WAL only.
func (n *node) restart(crash string) {
	n.t.Helper()
	if n.alive() {
		_ = n.cmd.Process.Kill()
		n.reap()
	} else if n.cmd != nil && n.cmd.ProcessState == nil {
		// Already dead (crash point) but not reaped yet.
		n.reap()
	}
	n.crash = crash
	n.stopped = false
	n.start()
}

func (n *node) cleanup() {
	n.kill()
}

// ---- HTTP helpers ----

var httpClient = &http.Client{Timeout: 5 * time.Second}

func postJSON(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	buf, _ := json.Marshal(body)
	resp, err := httpClient.Post(url, "application/json", bytes.NewReader(buf))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(data, &out)
	return resp.StatusCode, out
}

// postExpectError is like postJSON but tolerates connection errors (used
// while a node is expected to be crashing).
func postExpectError(t *testing.T, url string, body any) (int, error) {
	t.Helper()
	buf, _ := json.Marshal(body)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post(url, "application/json", bytes.NewReader(buf))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return resp.StatusCode, nil
}

func getJSON(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := httpClient.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(data, &out)
	return resp.StatusCode, out
}

// waitFor polls a GET until cond is true.
func waitFor(t *testing.T, url string, cond func(map[string]any) bool, timeout time.Duration) map[string]any {
	t.Helper()
	deadline := time.Now().Add(timeout)
	var last map[string]any
	for time.Now().Before(deadline) {
		resp, err := httpClient.Get(url)
		if err == nil {
			data, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			last = map[string]any{}
			_ = json.Unmarshal(data, &last)
			if resp.StatusCode == 200 && cond(last) {
				return last
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timeout waiting on %s; last=%v", url, last)
	return nil
}

// ---- cluster fixture ----

type cluster struct {
	t    *testing.T
	root string
	c    *node
	p1   *node
	p2   *node
}

func newCluster(t *testing.T) *cluster {
	t.Helper()
	root := t.TempDir()
	p1Port, p2Port := freePort(t), freePort(t)
	partsArg := fmt.Sprintf("p1=http://127.0.0.1:%d,p2=http://127.0.0.1:%d", p1Port, p2Port)

	cl := &cluster{
		t:    t,
		root: root,
		p1:   &node{t: t, role: "participant", name: "p1", port: p1Port, datadir: filepath.Join(root, "p1")},
		p2:   &node{t: t, role: "participant", name: "p2", port: p2Port, datadir: filepath.Join(root, "p2")},
	}
	cl.c = &node{
		t: t, role: "coordinator", name: "c1", port: freePort(t),
		datadir: filepath.Join(root, "c1"), partsArg: partsArg,
	}
	cl.p1.start()
	cl.p2.start()
	cl.c.start()
	t.Cleanup(func() { cl.c.cleanup(); cl.p1.cleanup(); cl.p2.cleanup() })
	return cl
}

func (cl *cluster) commitWrites(txid string) []map[string]string {
	return []map[string]string{
		{"participant": "p1", "key": "k1", "value": "v1-" + txid},
		{"participant": "p1", "key": "k2", "value": "v2-" + txid},
		{"participant": "p2", "key": "k3", "value": "v3-" + txid},
	}
}

func kv(cl *cluster, p *node, key string) string {
	code, resp := getJSON(cl.t, p.url()+"/kv/"+key)
	if code != 200 {
		cl.t.Fatalf("kv %s/%s = %d", p.name, key, code)
	}
	v, _ := resp["value"].(string)
	return v
}
