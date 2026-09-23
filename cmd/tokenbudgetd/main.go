// Command tokenbudgetd runs the layered global/tenant token-budget HTTP
// service.
//
// By default it uses the real monotonic clock. -virtual switches to a virtual
// clock (useful for demos and deterministic experiments), exposing
// POST /internal/clock/advance. Configuration is provided with -config (JSON)
// or built-in defaults; it can be replaced at runtime via PUT /v1/config.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"tokenbudget/internal/budget"
	"tokenbudget/internal/clock"
	"tokenbudget/internal/httpapi"
	"tokenbudget/internal/rational"
)

func defaultConfig() budget.Config {
	return budget.Config{
		Global:  budget.BucketConfig{Rate: rational.PerSecond(10), BurstMicro: 10 * budget.MicroPerToken},
		Default: budget.BucketConfig{Rate: rational.PerSecond(2), BurstMicro: 5 * budget.MicroPerToken},
	}
}

type fileConfig struct {
	Global  fileBucket            `json:"global"`
	Default fileBucket            `json:"default"`
	Tenants map[string]fileBucket `json:"tenants"`
}

type fileBucket struct {
	Rate  rational.Rate `json:"rate"`
	Burst string        `json:"burst"`
}

func loadConfig(path string) (budget.Config, error) {
	if path == "" {
		return defaultConfig(), nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return budget.Config{}, err
	}
	var fc fileConfig
	if err := json.Unmarshal(raw, &fc); err != nil {
		return budget.Config{}, err
	}
	g, err := convertBucket(fc.Global)
	if err != nil {
		return budget.Config{}, fmt.Errorf("global: %w", err)
	}
	d, err := convertBucket(fc.Default)
	if err != nil {
		return budget.Config{}, fmt.Errorf("default: %w", err)
	}
	cfg := budget.Config{Global: g, Default: d}
	if len(fc.Tenants) > 0 {
		cfg.Tenants = make(map[string]budget.BucketConfig, len(fc.Tenants))
		for t, fb := range fc.Tenants {
			bc, err := convertBucket(fb)
			if err != nil {
				return budget.Config{}, fmt.Errorf("tenant %q: %w", t, err)
			}
			cfg.Tenants[t] = bc
		}
	}
	if err := cfg.Validate(); err != nil {
		return budget.Config{}, err
	}
	return cfg, nil
}

func convertBucket(fb fileBucket) (budget.BucketConfig, error) {
	burst, err := rational.ParseDecimalTokens(fb.Burst)
	if err != nil {
		return budget.BucketConfig{}, err
	}
	if burst <= 0 {
		return budget.BucketConfig{}, fmt.Errorf("burst must be positive")
	}
	return budget.BucketConfig{Rate: fb.Rate, BurstMicro: burst}, nil
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	configPath := flag.String("config", "", "path to JSON config (defaults: global 10/s burst 10, tenant 2/s burst 5)")
	virtual := flag.Bool("virtual", false, "use a virtual clock (advance via POST /internal/clock/advance)")
	eventsPath := flag.String("events-file", "", "also append structured events as JSON lines to this file")
	flag.Parse()

	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	var clk clock.Clock
	var vclock *clock.Virtual
	if *virtual {
		vclock = clock.NewVirtual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
		clk = vclock
	} else {
		clk = clock.Real{}
	}

	memSink := budget.NewMemorySink(10_000)
	var sink budget.Sink = memSink
	var eventsFile *os.File
	if *eventsPath != "" {
		f, err := os.OpenFile(*eventsPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			log.Fatalf("events file: %v", err)
		}
		eventsFile = f
		sink = budget.MultiSink{memSink, budget.NewJSONLSink(f)}
	}

	limiter, err := budget.New(cfg, clk, sink)
	if err != nil {
		log.Fatalf("limiter: %v", err)
	}
	scheduler := budget.NewScheduler(limiter, budget.InlineExecutor{}, sink)
	srv := httpapi.NewServer(limiter, scheduler, memSink, clk, vclock)

	httpServer := &http.Server{Addr: *addr, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}

	go func() {
		log.Printf("tokenbudgetd listening on %s (virtual=%v)", *addr, *virtual)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	log.Printf("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(shutdownCtx)
	if eventsFile != nil {
		_ = eventsFile.Close()
	}
}
