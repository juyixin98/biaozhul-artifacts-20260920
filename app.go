package tailsampling

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// Run parses flags and runs the HTTP server. It is separated from main so
// tests can exercise the full wiring.
func Run(args []string) error {
	fs := flag.NewFlagSet("tailsampling", flag.ContinueOnError)
	configPath := fs.String("config", "", "path to JSON config file (optional)")
	listen := fs.String("listen", "", "HTTP listen address (e.g. :8080)")
	dataDir := fs.String("data-dir", "", "directory for JSONL logs and snapshots")
	waitWindow := fs.String("wait-window", "", "decision wait window, e.g. 2s")
	maxTTL := fs.String("max-ttl", "", "max trace buffering TTL, e.g. 10s")
	threshold := fs.Int64("latency-threshold-ms", 0, "tail-latency keep threshold in ms")
	rate := fs.Float64("probabilistic-rate", -1, "baseline keep rate in [0,1]")
	budgetCap := fs.Float64("budget-capacity", 0, "keep budget token bucket capacity")
	budgetRefill := fs.Float64("budget-refill-per-sec", -1, "keep budget refill tokens/sec")
	if err := fs.Parse(args); err != nil {
		return err
	}

	cfg := DefaultConfig()
	if *configPath != "" {
		c, err := LoadConfigFile(*configPath)
		if err != nil {
			return err
		}
		cfg = c
	}
	if err := cfg.applyEnv(); err != nil {
		return err
	}
	if *listen != "" {
		cfg.Listen = *listen
	}
	if *dataDir != "" {
		cfg.DataDir = *dataDir
	}
	if *waitWindow != "" {
		d, err := time.ParseDuration(*waitWindow)
		if err != nil {
			return fmt.Errorf("wait-window: %w", err)
		}
		cfg.WaitWindow = d
	}
	if *maxTTL != "" {
		d, err := time.ParseDuration(*maxTTL)
		if err != nil {
			return fmt.Errorf("max-ttl: %w", err)
		}
		cfg.MaxTTL = d
	}
	if *threshold > 0 {
		cfg.LatencyThresholdMs = *threshold
	}
	if *rate >= 0 {
		cfg.ProbabilisticRate = *rate
	}
	if *budgetCap > 0 {
		cfg.BudgetCapacity = *budgetCap
	}
	if *budgetRefill >= 0 {
		cfg.BudgetRefillPerSec = *budgetRefill
	}
	if err := cfg.Validate(); err != nil {
		return err
	}

	store, err := OpenFileStore(cfg.DataDir)
	if err != nil {
		return err
	}
	agg, err := NewAggregator(cfg, nil, store)
	if err != nil {
		_ = store.Close()
		return err
	}
	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           NewServer(agg).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	ln, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		agg.Close()
		_ = store.Close()
		return fmt.Errorf("listen %s: %w", cfg.Listen, err)
	}
	log.Printf("tail-sampling backend listening on %s (wait_window=%s max_ttl=%s data_dir=%s)",
		cfg.Listen, cfg.WaitWindow, cfg.MaxTTL, cfg.DataDir)

	serveErr := make(chan error, 1)
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			serveErr <- err
		}
	}()

	select {
	case err := <-serveErr:
		// Startup/serve failure (e.g. port in use): close resources cleanly.
		agg.Close()
		_ = store.Close()
		return fmt.Errorf("http server: %w", err)
	case <-stop:
	}
	log.Printf("shutting down: finalizing snapshot")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	agg.Close()
	return store.Close()
}
