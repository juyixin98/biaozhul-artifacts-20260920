// Command tokenbudget runs the local HTTP API for the two-layer token-budget
// scheduler. It is a pure backend: no UI is served.
package main

import (
	"context"
	"encoding/json"
	"errors"
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
	"tokenbudget/internal/event"
	"tokenbudget/internal/httpapi"
)

type fileConfig struct {
	Global        bucketFile            `json:"global"`
	DefaultTenant bucketFile            `json:"default_tenant"`
	Tenants       map[string]bucketFile `json:"tenants"`
}

type bucketFile struct {
	RateNum       int64  `json:"rate_num"`
	RateDenNS     int64  `json:"rate_den_ns"`
	Capacity      int64  `json:"capacity"`
	InitialTokens *int64 `json:"initial_tokens,omitempty"`
}

func (b bucketFile) cfg() budget.Config {
	return budget.Config{
		Rate:          budget.Rate{Num: b.RateNum, Den: b.RateDenNS},
		Capacity:      b.Capacity,
		InitialTokens: b.InitialTokens,
	}
}

func defaultConfig() fileConfig {
	return fileConfig{
		Global:        bucketFile{RateNum: 100, RateDenNS: int64(time.Second), Capacity: 100},
		DefaultTenant: bucketFile{RateNum: 10, RateDenNS: int64(time.Second), Capacity: 20},
		Tenants:       map[string]bucketFile{},
	}
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	configPath := flag.String("config", "", "path to JSON config (built-in defaults if empty)")
	eventsPath := flag.String("events", "", "optional path: append every event as JSON Lines")
	horizon := flag.Duration("horizon", 24*time.Hour, "max scheduled wait (0 disables)")
	flag.Parse()

	cfg := defaultConfig()
	if *configPath != "" {
		raw, err := os.ReadFile(*configPath)
		if err != nil {
			log.Fatalf("read config: %v", err)
		}
		if err := json.Unmarshal(raw, &cfg); err != nil {
			log.Fatalf("parse config: %v", err)
		}
	}

	clk := clock.NewRealClock()
	mem := &event.MemorySink{}
	var bus event.Bus
	if *eventsPath != "" {
		f, err := os.OpenFile(*eventsPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			log.Fatalf("open events file: %v", err)
		}
		defer f.Close()
		bus = event.NewBus(clk, mem, event.NewJSONSink(f))
	} else {
		bus = event.NewBus(clk, mem)
	}

	l, err := budget.NewLimiter(clk, bus, cfg.Global.cfg(), cfg.DefaultTenant.cfg())
	if err != nil {
		log.Fatalf("init limiter: %v", err)
	}
	for id, bc := range cfg.Tenants {
		if err := l.UpdateTenantConfig(id, bc.cfg()); err != nil {
			log.Fatalf("tenant %q: %v", id, err)
		}
	}

	srv := &httpapi.Server{
		Limiter:   l,
		Scheduler: budget.NewScheduler(l, clk, bus, int64(*horizon)),
		Mem:       mem,
	}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.NewMux(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("tokenbudget listening on %s (horizon=%s)", *addr, horizon.String())
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	fmt.Fprintln(os.Stderr, "stopped")
}
