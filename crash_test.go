package tpc_test

// Crash-boundary tests. Each test runs the real binaries as subprocesses,
// arms one crash point (a location immediately after a durable log write),
// lets the process die there, restarts it, and verifies the atomicity
// invariant: all participants end in the SAME final state (all COMMITTED or
// all ABORTED) — never a partial commit.

import (
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
)

var binDir string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "tpc-bin")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	binDir = dir
	for _, pkg := range []string{"tpc-coordinator", "tpc-participant"} {
		out := filepath.Join(dir, pkg)
		cmd := exec.Command("go", "build", "-o", out, "./cmd/"+pkg)
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			fmt.Fprintf(os.Stderr, "build %s: %v\n", pkg, err)
			os.Exit(1)
		}
	}
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// startNode launches a binary. Expected crashes (exit code 2) are fine.
func startNode(t *testing.T, bin string, args []string, env ...string) *exec.Cmd {
	t.Helper()
	cmd := exec.Command(filepath.Join(binDir, bin), args...)
	cmd.Env = append(os.Environ(), env...)
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		cmd.Process.Kill()
		cmd.Wait()
	})
	return cmd
}

func waitPort(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		c, err := net.DialTimeout("tcp", addr, 100*time.Millisecond)
		if err == nil {
			c.Close()
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("port %s never came up", addr)
}

func waitExit(t *testing.T, cmd *exec.Cmd) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("process did not exit (crash point not hit?)")
	}
}

func httpPost(url, body string) (int, string, error) {
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b), nil
}

// participantState polls /status until txID reaches want (or timeout fails).
func participantState(partURL, txID string) string {
	resp, err := http.Get(partURL + "/status")
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	var v struct {
		Txs map[string]struct {
			State string `json:"state"`
		} `json:"txs"`
	}
	if json.NewDecoder(resp.Body).Decode(&v) != nil {
		return ""
	}
	return v.Txs[txID].State
}

func waitState(t *testing.T, partURL, txID, want string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if participantState(partURL, txID) == want {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("%s: tx %s never reached %s (now %s)", partURL, txID, want, participantState(partURL, txID))
}

// assertConsistent is the atomicity invariant: every participant agrees on
// the outcome, and the outcome is a real terminal state.
func assertConsistent(t *testing.T, txID string, partURLs ...string) {
	t.Helper()
	states := map[string]bool{}
	for _, u := range partURLs {
		s := participantState(u, txID)
		if s != "COMMITTED" && s != "ABORTED" {
			t.Fatalf("%s: tx %s in non-terminal state %q", u, txID, s)
		}
		states[s] = true
	}
	if len(states) != 1 {
		t.Fatalf("PARTIAL OUTCOME for %s: participants disagree: %v", txID, states)
	}
	t.Logf("tx %s: all participants agree on %v", txID, states)
}

type cluster struct {
	dir       string
	coordAddr string
	coordURL  string
	coordWal  string
	partURLs  []string
	partAddrs []string
	partWals  []string
}

func newCluster(t *testing.T) *cluster {
	t.Helper()
	c := &cluster{dir: t.TempDir()}
	cp := freePort(t)
	c.coordAddr = fmt.Sprintf("127.0.0.1:%d", cp)
	c.coordURL = "http://" + c.coordAddr
	c.coordWal = filepath.Join(c.dir, "coordinator.wal")
	for i := 0; i < 2; i++ {
		p := freePort(t)
		addr := fmt.Sprintf("127.0.0.1:%d", p)
		c.partAddrs = append(c.partAddrs, addr)
		c.partURLs = append(c.partURLs, "http://"+addr)
		c.partWals = append(c.partWals, filepath.Join(c.dir, fmt.Sprintf("participant%d.wal", i)))
	}
	return c
}

func (c *cluster) startCoordinator(t *testing.T, crashEnv string) *exec.Cmd {
	args := []string{"-addr", c.coordAddr, "-log", c.coordWal}
	if crashEnv != "" {
		args = append(args, "-crash-after", crashEnv)
	}
	cmd := startNode(t, "tpc-coordinator", args)
	waitPort(t, c.coordAddr)
	return cmd
}

func (c *cluster) startParticipant(t *testing.T, i int, crashEnv string) *exec.Cmd {
	args := []string{"-addr", c.partAddrs[i], "-log", c.partWals[i], "-coordinator", c.coordURL}
	if crashEnv != "" {
		args = append(args, "-crash-after", crashEnv)
	}
	cmd := startNode(t, "tpc-participant", args)
	waitPort(t, c.partAddrs[i])
	return cmd
}

func (c *cluster) createTx(t *testing.T, id string) {
	body := fmt.Sprintf(`{"id":%q,"participants":[%q,%q],"payload":{"resource":"r1"}}`,
		id, c.partURLs[0], c.partURLs[1])
	code, resp, err := httpPost(c.coordURL+"/tx", body)
	if err != nil || code != http.StatusCreated {
		t.Fatalf("create tx: code=%d err=%v resp=%s", code, err, resp)
	}
}

// Crash after PREPARING is durable, before the create RPC returns.
func TestCrashAfterPreparing(t *testing.T) {
	c := newCluster(t)
	c.startParticipant(t, 0, "")
	c.startParticipant(t, 1, "")
	coord := c.startCoordinator(t, "c:after-preparing")

	httpPost(c.coordURL+"/tx", `{"id":"tx1","participants":["`+c.partURLs[0]+`","`+c.partURLs[1]+`"],"payload":{}}`)
	waitExit(t, coord) // died right after fsync(PREPARING)

	// Restart: no decision was durable, so recovery must abort.
	c.startCoordinator(t, "")
	waitState(t, c.partURLs[0], "tx1", "ABORTED")
	waitState(t, c.partURLs[1], "tx1", "ABORTED")
	assertConsistent(t, "tx1", c.partURLs...)
}

// Crash after all yes votes but BEFORE the decision is logged.
// Participants are PREPARED and must stay blocked — never self-abort —
// until the restarted coordinator decides (abort) and tells them.
func TestCrashAfterAllPrepared(t *testing.T) {
	c := newCluster(t)
	c.startParticipant(t, 0, "")
	c.startParticipant(t, 1, "")
	coord := c.startCoordinator(t, "c:after-all-prepared")

	c.createTx(t, "tx1")
	go httpPost(c.coordURL+"/tx/tx1/commit", "")
	waitExit(t, coord) // died with the decision not yet written

	// Coordinator is down. Wait well past any timeout: participants must
	// remain PREPARED (blocked) and must not abort on their own.
	time.Sleep(1200 * time.Millisecond)
	for _, u := range c.partURLs {
		if s := participantState(u, "tx1"); s != "PREPARED" {
			t.Fatalf("%s: prepared participant must not self-abort, state=%s", u, s)
		}
	}

	// Coordinator returns; recovery finds no durable decision -> abort.
	c.startCoordinator(t, "")
	waitState(t, c.partURLs[0], "tx1", "ABORTED")
	waitState(t, c.partURLs[1], "tx1", "ABORTED")
	assertConsistent(t, "tx1", c.partURLs...)
}

// Crash after the COMMIT decision is durable, before anyone is notified.
func TestCrashAfterCommitDecision(t *testing.T) {
	c := newCluster(t)
	c.startParticipant(t, 0, "")
	c.startParticipant(t, 1, "")
	coord := c.startCoordinator(t, "c:after-decision")

	c.createTx(t, "tx1")
	go httpPost(c.coordURL+"/tx/tx1/commit", "")
	waitExit(t, coord) // died right after fsync(COMMIT)

	// Restart: recovery must re-deliver COMMIT to both participants.
	c.startCoordinator(t, "")
	waitState(t, c.partURLs[0], "tx1", "COMMITTED")
	waitState(t, c.partURLs[1], "tx1", "COMMITTED")
	assertConsistent(t, "tx1", c.partURLs...)
}

// Participant dies after fsync(PREPARED) but before its yes vote reaches
// the coordinator. The coordinator must abort; the restarted participant
// must learn the abort from the coordinator (not from any local timeout).
func TestCrashParticipantAfterPrepared(t *testing.T) {
	c := newCluster(t)
	p0 := c.startParticipant(t, 0, "p:after-prepared")
	c.startParticipant(t, 1, "")
	c.startCoordinator(t, "")

	c.createTx(t, "tx1")
	commitDone := make(chan struct{})
	go func() { httpPost(c.coordURL+"/tx/tx1/commit", ""); close(commitDone) }()
	waitExit(t, p0) // died after fsync(PREPARED), before replying

	// Coordinator sees a failed prepare -> abort. Participant 1 aborts now.
	waitState(t, c.partURLs[1], "tx1", "ABORTED")

	// Restart participant 0: it recovers PREPARED, asks the coordinator,
	// and applies the abort.
	c.startParticipant(t, 0, "")
	waitState(t, c.partURLs[0], "tx1", "ABORTED")
	<-commitDone
	assertConsistent(t, "tx1", c.partURLs...)
}

// Participant dies after fsync(COMMITTED) but before acking. The
// coordinator retries; the restarted participant answers idempotently.
func TestCrashParticipantAfterCommitted(t *testing.T) {
	c := newCluster(t)
	p0 := c.startParticipant(t, 0, "p:after-committed")
	c.startParticipant(t, 1, "")
	c.startCoordinator(t, "")

	c.createTx(t, "tx1")
	commitDone := make(chan struct{})
	go func() { httpPost(c.coordURL+"/tx/tx1/commit", ""); close(commitDone) }()
	waitExit(t, p0) // died after fsync(COMMITTED), before acking

	// The coordinator is now retrying commit to participant 0 forever.
	c.startParticipant(t, 0, "")
	waitState(t, c.partURLs[0], "tx1", "COMMITTED")
	waitState(t, c.partURLs[1], "tx1", "COMMITTED")
	<-commitDone
	assertConsistent(t, "tx1", c.partURLs...)
}

// Participant dies after fsync(ABORTED) but before acking the abort.
func TestCrashParticipantAfterAborted(t *testing.T) {
	c := newCluster(t)
	p0 := c.startParticipant(t, 0, "p:after-aborted")
	c.startParticipant(t, 1, "")
	c.startCoordinator(t, "")

	// vote:no makes the prepare fail, so the coordinator decides abort and
	// notifies participant 0, which dies right after logging ABORTED.
	body := fmt.Sprintf(`{"id":"tx1","participants":[%q,%q],"payload":{"vote":"no"}}`, c.partURLs[0], c.partURLs[1])
	if code, resp, err := httpPost(c.coordURL+"/tx", body); err != nil || code != http.StatusCreated {
		t.Fatalf("create: %d %v %s", code, err, resp)
	}
	commitDone := make(chan struct{})
	go func() { httpPost(c.coordURL+"/tx/tx1/commit", ""); close(commitDone) }()
	waitExit(t, p0)

	c.startParticipant(t, 0, "")
	waitState(t, c.partURLs[0], "tx1", "ABORTED")
	waitState(t, c.partURLs[1], "tx1", "ABORTED")
	<-commitDone
	assertConsistent(t, "tx1", c.partURLs...)
}
