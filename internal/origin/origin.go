// Package origin starts the range service on a local loopback listener.
// It exists so both the HTTP server binary and the self-test harness bring
// up the service the same way: a real in-process HTTP server on a real port,
// with no external or production system involved.
package origin

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"time"

	"httprange/internal/artifact"
	"httprange/internal/clock"
	"httprange/internal/server"
)

// Handle is a started local service.
type Handle struct {
	BaseURL string
	Store   *artifact.Store
	Clk     clock.Clock
	srv     *http.Server
	ln      net.Listener
}

// Config configures a started service.
type Config struct {
	Addr       string // listen address; "" binds 127.0.0.1 on a free port
	Store      *artifact.Store
	Clock      clock.Clock // defaults to a fixed fake clock
	EnableGzip bool
	MaxRanges  int
}

// Start binds the configured address and serves until ctx is canceled.
func Start(ctx context.Context, cfg Config) (*Handle, error) {
	if cfg.Store == nil {
		cfg.Store = artifact.NewStore()
	}
	if cfg.Clock == nil {
		cfg.Clock = clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	}
	listenAddr := cfg.Addr
	if listenAddr == "" {
		listenAddr = "127.0.0.1:0"
	}

	ln, err := net.Listen("tcp", listenAddr)
	if err != nil {
		return nil, fmt.Errorf("origin: listen: %w", err)
	}

	opts := []server.Option{}
	if cfg.EnableGzip {
		opts = append(opts, server.WithGzip())
	}
	if cfg.MaxRanges > 0 {
		opts = append(opts, server.WithMaxRanges(cfg.MaxRanges))
	}
	h := server.New(cfg.Store, cfg.Clock, opts...)
	srv := &http.Server{
		Handler:           h.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() { _ = srv.Serve(ln) }()

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	return &Handle{
		BaseURL: "http://" + ln.Addr().String(),
		Store:   cfg.Store,
		Clk:     cfg.Clock,
		srv:     srv,
		ln:      ln,
	}, nil
}

// Shutdown stops the HTTP service.
func (h *Handle) Shutdown() error { return h.srv.Close() }
