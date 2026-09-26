package e2e

import (
	"bufio"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

// proc wraps a re-executed test binary (a real OS subprocess). It is used
// to verify hard-crash behavior: os.Exit in the child is a genuine process
// death, not an in-process mock.
type proc struct {
	cmd    *exec.Cmd
	stdout io.ReadCloser
	exited chan struct{}
	mu     sync.Mutex
	code   int
}

// waitReady reads worker stdout until a line beginning "READY ". Leading
// test-framework lines (e.g. -test.v's "=== RUN") are skipped.
func (p *proc) waitReady() (string, error) {
	br := bufio.NewReader(p.stdout)
	type lineErr struct {
		line string
		err  error
	}
	ch := make(chan lineErr, 1)
	go func() {
		for {
			line, err := br.ReadString('\n')
			if strings.HasPrefix(line, "READY ") {
				ch <- lineErr{line: line, err: err}
				return
			}
			if err != nil {
				ch <- lineErr{line: line, err: err}
				return
			}
		}
	}()
	select {
	case r := <-ch:
		if strings.HasPrefix(r.line, "READY ") {
			return r.line, nil
		}
		return "", r.err
	case <-p.exited:
		return "", errors.New("worker exited before READY")
	case <-time.After(5 * time.Second):
		return "", errors.New("timeout waiting for worker READY")
	}
}

func (p *proc) waitExit(timeout time.Duration) int {
	select {
	case <-p.exited:
	case <-time.After(timeout):
		_ = p.killErr()
		<-p.exited
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.code
}

func (p *proc) killErr() error {
	if p.cmd.Process == nil {
		return nil
	}
	return p.cmd.Process.Kill()
}

func (p *proc) kill() {
	_ = p.killErr()
	<-p.exited
}

// startTestBinary re-executes the current test binary with the given args.
func startTestBinary(args []string, env ...string) *proc {
	exe, err := os.Executable()
	if err != nil {
		panic(err)
	}
	cmd := exec.Command(exe, args...)
	cmd.Env = append(os.Environ(), env...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		panic(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		panic(err)
	}
	p := &proc{cmd: cmd, stdout: stdout, exited: make(chan struct{})}
	go func() {
		waitErr := cmd.Wait()
		p.mu.Lock()
		p.code = exitCode(waitErr)
		p.mu.Unlock()
		close(p.exited)
	}()
	return p
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		if s, ok := ee.Sys().(syscall.WaitStatus); ok {
			return s.ExitStatus()
		}
	}
	return -1
}
