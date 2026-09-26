package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

// proc 表示一个被托管在子进程里的本地服务。
type proc struct {
	name    string
	binPath string
	args    []string
	logPath string

	mu      sync.Mutex
	cmd     *exec.Cmd
	logFile *os.File
	alive   bool
}

func startProc(ctx context.Context, name, binPath, logPath string, args ...string) (*proc, error) {
	if err := os.MkdirAll(filepath.Dir(logPath), 0o755); err != nil {
		return nil, fmt.Errorf("create log dir for %s: %w", name, err)
	}
	lf, err := os.Create(logPath)
	if err != nil {
		return nil, fmt.Errorf("open %s log: %w", name, err)
	}
	cmd := exec.CommandContext(ctx, binPath, args...)
	cmd.Stdout = lf
	cmd.Stderr = lf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = lf.Close()
		return nil, fmt.Errorf("start %s: %w", name, err)
	}
	return &proc{name: name, binPath: binPath, args: args, logPath: logPath, cmd: cmd, logFile: lf, alive: true}, nil
}

// kill SIGKILL 整个进程组（崩溃模拟）。
func (p *proc) kill() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.alive || p.cmd.Process == nil {
		return nil
	}
	_ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL)
	_, _ = p.cmd.Process.Wait()
	p.alive = false
	if p.logFile != nil {
		_ = p.logFile.Close()
		p.logFile = nil
	}
	return nil
}

// stop 发送 SIGTERM 优雅关停。
func (p *proc) stop() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.alive || p.cmd.Process == nil {
		return nil
	}
	_ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGTERM)
	done := make(chan struct{})
	go func() { _, _ = p.cmd.Process.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(6 * time.Second):
		if p.cmd.Process != nil {
			_ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL)
		}
	}
	p.alive = false
	if p.logFile != nil {
		_ = p.logFile.Close()
		p.logFile = nil
	}
	return nil
}

// restart 强杀后以相同参数重新拉起。
func (p *proc) restart(ctx context.Context) error {
	if err := p.kill(); err != nil {
		return err
	}
	lf, err := os.Create(p.logPath)
	if err != nil {
		return fmt.Errorf("reopen %s log: %w", p.name, err)
	}
	cmd := exec.CommandContext(ctx, p.binPath, p.args...)
	cmd.Stdout = lf
	cmd.Stderr = lf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = lf.Close()
		return fmt.Errorf("restart %s: %w", p.name, err)
	}
	p.mu.Lock()
	p.cmd = cmd
	p.logFile = lf
	p.alive = true
	p.mu.Unlock()
	return nil
}

func (p *proc) tail(n int) string {
	b, err := os.ReadFile(p.logPath)
	if err != nil {
		return ""
	}
	if len(b) > n {
		b = b[len(b)-n:]
	}
	return string(b)
}

// Env 是整个验收环境：常驻假审计进程 + 可重启的业务进程。
type Env struct {
	workDir string

	auditProc *proc
	bizProc   *proc

	bizBin   string
	auditBin string

	bizAddr   string
	auditAddr string

	dataDir string
	ttl     time.Duration
	waitMax time.Duration
}

// NewEnv 编译两个服务二进制并准备（但尚未启动）环境。
func NewEnv(ctx context.Context, workDir string, ttl, waitMax time.Duration) (*Env, error) {
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return nil, err
	}
	bizBin, err := buildBinary(workDir, "idempotency-server", "./cmd/server")
	if err != nil {
		return nil, err
	}
	auditBin, err := buildBinary(workDir, "auditserver", "./cmd/auditserver")
	if err != nil {
		return nil, err
	}
	bizAddr, err := freeAddr()
	if err != nil {
		return nil, err
	}
	auditAddr, err := freeAddr()
	if err != nil {
		return nil, err
	}
	dataDir := filepath.Join(workDir, "data")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return nil, err
	}
	return &Env{
		workDir: workDir, bizBin: bizBin, auditBin: auditBin,
		bizAddr: bizAddr, auditAddr: auditAddr, dataDir: dataDir,
		ttl: ttl, waitMax: waitMax,
	}, nil
}

// Start 启动假审计（常驻）与业务服务。
func (e *Env) Start(ctx context.Context) error {
	var err error
	e.auditProc, err = startProc(ctx, "auditserver", e.auditBin,
		filepath.Join(e.workDir, "auditserver.log"), "-addr", e.auditAddr)
	if err != nil {
		return err
	}
	e.bizProc, err = startProc(ctx, "idempotency-server", e.bizBin,
		filepath.Join(e.workDir, "server.log"),
		"-addr", e.bizAddr,
		"-audit-base-url", "http://"+e.auditAddr,
		"-data-dir", e.dataDir,
		"-fake-clock",
		"-pending-ttl", e.ttl.String(),
		"-wait-max", e.waitMax.String(),
	)
	if err != nil {
		_ = e.auditProc.kill()
		return err
	}
	if err := waitHealthy("http://"+e.bizAddr+"/healthz", 10*time.Second); err != nil {
		return err
	}
	return nil
}

func (e *Env) BizURL() string   { return "http://" + e.bizAddr }
func (e *Env) AuditURL() string { return "http://" + e.auditAddr }

// CrashBiz 强杀业务进程（审计进程存活）。
func (e *Env) CrashBiz() error { return e.bizProc.kill() }

// RestartBiz 重启业务进程并重放同一 WAL。
func (e *Env) RestartBiz(ctx context.Context) error {
	if err := e.bizProc.restart(ctx); err != nil {
		return err
	}
	return waitHealthy("http://"+e.bizAddr+"/healthz", 10*time.Second)
}

// Stop 关停所有进程。
func (e *Env) Stop() {
	_ = e.bizProc.stop()
	_ = e.auditProc.stop()
}

func (e *Env) BizLogTail() string   { return e.bizProc.tail(4000) }
func (e *Env) AuditLogTail() string { return e.auditProc.tail(2000) }

func freeAddr() (string, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	defer l.Close()
	return l.Addr().String(), nil
}

func waitHealthy(url string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	c := &http.Client{Timeout: 2 * time.Second}
	var lastErr error
	for time.Now().Before(deadline) {
		resp, err := c.Get(url)
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == 200 {
				return nil
			}
		}
		lastErr = err
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("health check %s timed out after %s: %v", url, timeout, lastErr)
}

func buildBinary(dir, name, pkg string) (string, error) {
	out := filepath.Join(dir, name)
	cmd := exec.Command("go", "build", "-o", out, pkg)
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return "", err
	}
	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("go build %s: %w", pkg, err)
	}
	errTail, _ := io.ReadAll(stderr)
	if err := cmd.Wait(); err != nil {
		return "", fmt.Errorf("go build %s failed: %v: %s", pkg, err, string(errTail))
	}
	return out, nil
}
