// Package app wires the HTTP service, fault-injected dependency and the
// four-phase shutdown coordinator together. Everything binds to localhost.
package app

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"sync/atomic"
	"time"

	"gracefulshutdown/internal/fakedep"
	"gracefulshutdown/internal/lifecycle"
	"gracefulshutdown/internal/shutdown"
)

// Config configures the application.
type Config struct {
	PublicAddr    string        // traffic listener, e.g. "127.0.0.1:18080"
	AdminAddr     string        // probe listener, e.g. "127.0.0.1:18081"
	RejectWindow  time.Duration // phase-1 window where the listener stays open but every new request gets HTTP 503
	DrainTimeout  time.Duration
	CancelTimeout time.Duration
	CloseTimeout  time.Duration
}

// DefaultConfig returns localhost defaults with short, demo-friendly budgets.
func DefaultConfig() Config {
	return Config{
		PublicAddr:    "127.0.0.1:18080",
		AdminAddr:     "127.0.0.1:18081",
		RejectWindow:  500 * time.Millisecond,
		DrainTimeout:  5 * time.Second,
		CancelTimeout: 2 * time.Second,
		CloseTimeout:  2 * time.Second,
	}
}

// App is the running service.
type App struct {
	cfg   Config
	coord *shutdown.Coordinator
	dep   *fakedep.Server

	pubLn, admLn net.Listener
	pubSrv       *http.Server
	admSrv       *http.Server
	client       *http.Client

	workRoot   context.Context
	workCancel context.CancelFunc
	ids        atomic.Uint64
}

// New constructs but does not start the app.
func New(cfg Config) *App {
	coord := shutdown.New(shutdown.Config{
		DrainTimeout:  cfg.DrainTimeout,
		CancelTimeout: cfg.CancelTimeout,
		CloseTimeout:  cfg.CloseTimeout,
	})
	dep := fakedep.New()

	root, cancel := context.WithCancel(context.Background())
	a := &App{
		cfg:        cfg,
		coord:      coord,
		dep:        dep,
		client:     &http.Client{Timeout: 30 * time.Second},
		workRoot:   root,
		workCancel: cancel,
	}

	pubMux := http.NewServeMux()
	pubMux.HandleFunc("/work", a.handleWork)
	pubMux.HandleFunc("/stream", a.handleStream)
	pubMux.HandleFunc("/bg", a.handleBackground)
	a.pubSrv = &http.Server{
		Addr:              cfg.PublicAddr,
		Handler:           pubMux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	admMux := http.NewServeMux()
	admMux.HandleFunc("/readyz", a.handleReady)
	admMux.HandleFunc("/livez", a.handleLive)
	admMux.HandleFunc("/report", a.handleReport)
	admMux.HandleFunc("/trigger-shutdown", a.handleTrigger)
	admMux.HandleFunc("/fault", a.handleFault)
	admMux.HandleFunc("/fault/release", a.handleFaultRelease)
	a.admSrv = &http.Server{
		Addr:              cfg.AdminAddr,
		Handler:           admMux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	// Phase 3 cancels the root context of all accepted work.
	go func() {
		<-coord.Cancelled()
		cancel()
	}()

	// Phase-4 resources close in reverse registration order, so:
	//   1. admin-http-listener (control/probe plane stops first),
	//   2. http-client            (no new outbound calls),
	//   3. fake-external-dependency (the thing calls were made to).
	// The admin listener can be a normal ordered resource because the
	// /trigger-shutdown handler hijacks its connection: a hijacked connection
	// is not tracked by http.Server.Shutdown, so the trigger request never
	// waits on its own listener being closed.
	coord.RegisterResource(resourceFunc{
		name: "fake-external-dependency",
		fn:   dep.Close,
	})
	coord.RegisterResource(resourceFunc{
		name: "http-client",
		fn: func(context.Context) error {
			a.client.CloseIdleConnections()
			return nil
		},
	})
	coord.RegisterResource(resourceFunc{
		name: "admin-http-listener",
		fn:   a.closeAdmin,
	})

	// Phase 1: readiness flips immediately (the phase transition does that);
	// the traffic listener stays open for RejectWindow so stragglers receive
	// an explicit HTTP 503 rejection (and background spawns are counted)
	// instead of a bare connection refusal — the same shape as a load
	// balancer's preStop drain window. Then Shutdown stops accepting for real.
	// It outlives phases 2 and 3; only work ignoring cancellation past the
	// full budget is force-closed.
	coord.OnStopAccept(func() {
		go func() {
			if cfg.RejectWindow > 0 {
				time.Sleep(cfg.RejectWindow)
			}
			ctx, cancel := context.WithTimeout(context.Background(),
				cfg.DrainTimeout+cfg.CancelTimeout+time.Second)
			defer cancel()
			if err := a.pubSrv.Shutdown(ctx); err != nil {
				// Work that ignored cancellation past the full budget: drop it.
				_ = a.pubSrv.Close()
			}
		}()
	})

	return a
}

// Dep exposes the fake dependency for fault injection.
func (a *App) Dep() *fakedep.Server { return a.dep }

// Coordinator exposes the coordinator (tests, signal wiring).
func (a *App) Coordinator() *shutdown.Coordinator { return a.coord }

// PublicURL / AdminURL report the bound base URLs.
func (a *App) PublicURL() string { return "http://" + a.pubLn.Addr().String() }
func (a *App) AdminURL() string  { return "http://" + a.admLn.Addr().String() }

// Start binds both listeners and begins serving.
func (a *App) Start() error {
	pubLn, err := net.Listen("tcp", a.cfg.PublicAddr)
	if err != nil {
		return fmt.Errorf("bind public listener %s: %w", a.cfg.PublicAddr, err)
	}
	a.pubLn = pubLn
	admLn, err := net.Listen("tcp", a.cfg.AdminAddr)
	if err != nil {
		_ = pubLn.Close()
		return fmt.Errorf("bind admin listener %s: %w", a.cfg.AdminAddr, err)
	}
	a.admLn = admLn

	go func() { _ = a.pubSrv.Serve(pubLn) }()
	go func() { _ = a.admSrv.Serve(admLn) }()
	return nil
}

// Shutdown runs one shutdown signal through the four phases and returns the
// structured report. Repeated calls represent repeated signals.
func (a *App) Shutdown() lifecycle.Report {
	return a.coord.Signal()
}

// newID issues short request ids unique within the process.
func (a *App) newID(prefix string) string {
	n := a.ids.Add(1)
	return fmt.Sprintf("%s-%04d", prefix, n)
}

// workContext returns a context that dies in phase 3 (or when the request ends).
func (a *App) workContext(r *http.Request) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(a.workRoot)
	go func() {
		select {
		case <-r.Context().Done():
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, cancel
}

// resourceFunc adapts a function to the shutdown.Resource interface.
type resourceFunc struct {
	name string
	fn   func(ctx context.Context) error
}

func (r resourceFunc) Name() string                    { return r.name }
func (r resourceFunc) Close(ctx context.Context) error { return r.fn(ctx) }

// adminCloseGrace bounds the graceful wait before idle admin (probe)
// connections are dropped. It is deliberately short: the control plane is
// being torn down and lingering keep-alive probe connections should not hold
// the whole shutdown hostage for the full close budget.
const adminCloseGrace = 250 * time.Millisecond

// closeAdmin stops the probe/admin listener. It gives in-flight responses a
// short graceful window, then force-closes. The /trigger-shutdown connection
// is hijacked and therefore neither waited on by Shutdown nor closed by Close,
// so the triggering client still receives the final report.
func (a *App) closeAdmin(ctx context.Context) error {
	gctx, cancel := context.WithTimeout(ctx, adminCloseGrace)
	err := a.admSrv.Shutdown(gctx)
	cancel()
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		_ = a.admSrv.Close() // drop idle keep-alive conns; hijacked conns are spared
		return nil
	}
	if err != nil {
		_ = a.admSrv.Close()
		return err
	}
	return nil
}
