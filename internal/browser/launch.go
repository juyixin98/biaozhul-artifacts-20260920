package browser

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/cdproto/runtime"
	"github.com/chromedp/chromedp"
)

// Instance is one running Chromium process shared by many serial collections
// (browser-level pool). Concurrency across instances is limited by the worker
// semaphore; a crashed browser is relaunched.
type Instance struct {
	mu sync.Mutex

	allocCancel context.CancelFunc
	browserCtx  context.Context

	// launched process bookkeeping (nil when attached to external Chrome)
	cmd        *exec.Cmd
	tmpDir     string
	wsURL      string // discovered DevTools endpoint of a launched process
	externalWS string // configured external Chrome endpoint (never relaunched)

	chromePath  string
	headless    bool
	crashedCh   chan struct{}
	crashedOnce sync.Once
}

// Options configures a browser instance.
type Options struct {
	ChromePath string
	Headless   bool
	// WSURL attaches to an external Chrome instead of launching one.
	WSURL string
}

// NewInstance constructs an instance; call Start before collecting.
func NewInstance(opts Options) *Instance {
	return &Instance{
		chromePath: opts.ChromePath,
		headless:   opts.Headless,
		externalWS: opts.WSURL,
		crashedCh:  make(chan struct{}),
	}
}

// wsEndpoint returns the external endpoint if one was configured, otherwise
// the endpoint discovered from a locally launched process.
func (i *Instance) wsEndpoint() string {
	if i.externalWS != "" {
		return i.externalWS
	}
	return i.wsURL
}

// Start launches (or attaches to) Chromium.
func (i *Instance) Start(parent context.Context) error {
	i.mu.Lock()
	defer i.mu.Unlock()

	if endpoint := i.wsEndpoint(); endpoint != "" {
		allocCtx, allocCancel := chromedp.NewRemoteAllocator(context.Background(), endpoint)
		ctx, cancel := chromedp.NewContext(allocCtx)
		i.allocCancel = func() {
			cancel()
			allocCancel()
		}
		i.browserCtx = ctx
		// Verify connectivity and force the browser to be initialized.
		if err := chromedp.Run(ctx, chromedp.ActionFunc(func(ctx context.Context) error {
			_, exp, err := runtime.Evaluate("1").Do(ctx)
			if err != nil {
				return err
			}
			if exp != nil {
				return fmt.Errorf("browser check exception: %s", exp.Text)
			}
			return nil
		})); err != nil {
			cancel()
			return fail(CodeBrowserLaunch, "connect external Chrome %s: %v", endpoint, err)
		}
		return nil
	}

	port, err := freePort()
	if err != nil {
		return fail(CodeBrowserLaunch, "find free port: %v", err)
	}
	tmpDir, err := os.MkdirTemp("", "sitevitals-chrome-")
	if err != nil {
		return fail(CodeBrowserLaunch, "temp dir: %v", err)
	}

	chromePath := i.chromePath
	if chromePath == "" {
		chromePath, err = exec.LookPath("chromium")
		if err != nil {
			chromePath, err = exec.LookPath("chromium-browser")
		}
		if err != nil {
			chromePath, err = exec.LookPath("google-chrome")
		}
		if err != nil {
			_ = os.RemoveAll(tmpDir)
			return fail(CodeBrowserLaunch, "chromium executable not found (set CHROME_PATH): %v", err)
		}
	}

	args := []string{
		"--remote-debugging-port=" + strconv.Itoa(port),
		"--user-data-dir=" + tmpDir,
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-extensions",
		"--disable-background-networking",
		"--disable-sync",
		"--mute-audio",
		"--disable-dev-shm-usage",
		"--disable-gpu",
		"--hide-scrollbars",
		"about:blank",
	}
	if i.headless {
		args = append([]string{"--headless=new", "--no-sandbox"}, args...)
	}

	cmd := exec.Command(chromePath, args...)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		_ = os.RemoveAll(tmpDir)
		return fail(CodeBrowserLaunch, "start %s: %v", chromePath, err)
	}
	i.cmd = cmd
	i.tmpDir = tmpDir

	wsURL, err := waitForDevTools(context.Background(), "127.0.0.1", port, 20*time.Second)
	if err != nil {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
		_ = os.RemoveAll(tmpDir)
		return fail(CodeBrowserLaunch, "devtools never came up: %v", err)
	}

	// Watch for unexpected process exit -> marks the instance crashed.
	go func() {
		err := cmd.Wait()
		i.crashedOnce.Do(func() {
			close(i.crashedCh)
		})
		_ = err
	}()

	allocCtx, _ := chromedp.NewRemoteAllocator(context.Background(), wsURL)
	ctx, cancel := chromedp.NewContext(allocCtx)
	i.allocCancel = cancel
	i.browserCtx = ctx
	i.wsURL = wsURL
	return nil
}

// Crashed is closed if the browser process exited unexpectedly.
func (i *Instance) Crashed() <-chan struct{} { return i.crashedCh }

// Context returns the chromedp browser context (callers derive tab contexts).
func (i *Instance) Context() context.Context { return i.browserCtx }

// Restart tears down and relaunches the browser after a crash. A launched
// instance forgets its stale DevTools endpoint first, otherwise Start would
// try to re-attach to the dead process.
func (i *Instance) Restart(parent context.Context) error {
	i.Close()
	i.mu.Lock()
	i.crashedCh = make(chan struct{})
	i.crashedOnce = sync.Once{}
	i.wsURL = "" // force a fresh process launch on Start
	i.browserCtx = nil
	i.allocCancel = nil
	i.cmd = nil
	i.tmpDir = ""
	i.mu.Unlock()
	return i.Start(parent)
}

// Close terminates the browser process and removes its profile.
func (i *Instance) Close() error {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.allocCancel != nil {
		i.allocCancel()
		i.allocCancel = nil
	}
	if i.cmd != nil && i.cmd.Process != nil {
		_ = i.cmd.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { _, _ = i.cmd.Process.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			_ = i.cmd.Process.Kill()
			<-done
		}
		i.cmd = nil
	}
	if i.tmpDir != "" {
		_ = os.RemoveAll(i.tmpDir)
		i.tmpDir = ""
	}
	return nil
}

// freePort asks the kernel for a free TCP port.
func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

// waitForDevTools polls /json/version until Chrome reports its ws endpoint.
func waitForDevTools(ctx context.Context, host string, port int, timeout time.Duration) (string, error) {
	url := fmt.Sprintf("http://%s:%d/json/version", host, port)
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: time.Second}
	for time.Now().Before(deadline) {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		resp, err := client.Do(req)
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			// Lightweight parse without encoding/json import churn.
			s := string(body)
			if idx := strings.Index(s, `"webSocketDebuggerUrl"`); idx >= 0 {
				rest := s[idx:]
				q1 := strings.Index(rest, `"ws://`)
				if q1 < 0 {
					q1 = strings.Index(rest, `"wss://`)
				}
				if q1 >= 0 {
					tail := rest[q1+1:]
					q2 := strings.Index(tail, `"`)
					if q2 > 0 {
						return tail[:q2], nil
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
	return "", fmt.Errorf("timeout after %s", timeout)
}
