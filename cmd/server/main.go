// Command server 启动基数预算治理 HTTP 服务。
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"cardinalgov/internal/governor"
	"cardinalgov/internal/httpapi"
)

func parseBudgets(spec string) map[string]int {
	out := map[string]int{}
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return out
	}
	for _, pair := range strings.Split(spec, ",") {
		name, val, ok := strings.Cut(pair, "=")
		if !ok {
			log.Fatalf("bad --metric-budgets entry %q, want metric=n", pair)
		}
		n, err := strconv.Atoi(strings.TrimSpace(val))
		if err != nil || n < 0 {
			log.Fatalf("bad --metric-budgets value in %q", pair)
		}
		out[strings.TrimSpace(name)] = n
	}
	return out
}

func main() {
	addr := flag.String("addr", envOr("ADDR", "127.0.0.1:8080"), "HTTP listen address (env ADDR)")
	seriesBudget := flag.Int("series-budget", envIntOr("SERIES_BUDGET", 10000), "default per-metric label-set budget")
	metricBudgets := flag.String("metric-budgets", os.Getenv("METRIC_BUDGETS"), "per-metric overrides, e.g. http_requests=50,errors=200")
	maxMetricNames := flag.Int("max-metric-names", envIntOr("MAX_METRIC_NAMES", 512), "global distinct metric name cap")
	maxLabelValue := flag.Int("max-label-value-bytes", envIntOr("MAX_LABEL_VALUE_BYTES", 256), "max label value bytes")
	truncate := flag.Bool("truncate-values", envBoolOr("TRUNCATE_VALUES", true), "truncate over-long label values (false = reject)")
	snapshotPath := flag.String("snapshot", envOr("SNAPSHOT_PATH", ""), "snapshot file path; empty disables persistence")
	flushInterval := flag.Duration("flush-interval", envDurationOr("FLUSH_INTERVAL", 5*time.Second), "periodic snapshot interval")
	flag.Parse()

	cfg := governor.DefaultConfig()
	cfg.DefaultSeriesBudget = *seriesBudget
	cfg.MetricBudgets = parseBudgets(*metricBudgets)
	cfg.MaxMetricNames = *maxMetricNames
	cfg.MaxLabelValueBytes = *maxLabelValue
	cfg.TruncateLabelValues = *truncate

	store := governor.NewStore(cfg)
	if *snapshotPath != "" {
		if _, err := os.Stat(*snapshotPath); err == nil {
			restored, err := governor.LoadSnapshot(*snapshotPath)
			if err != nil {
				log.Fatalf("load snapshot %s: %v", *snapshotPath, err)
			}
			store = restored
			st := store.Stats()
			log.Printf("restored snapshot: metrics=%d tracked_series=%d received=%d overflowed=%d rejected=%d",
				st.MetricNames, st.TrackedSeries, st.Counters.Received, st.Counters.Overflowed, st.Counters.Rejected)
		} else if !os.IsNotExist(err) {
			log.Fatalf("stat snapshot %s: %v", *snapshotPath, err)
		}
	}

	srv := &httpapi.Server{Store: store}
	if *snapshotPath != "" {
		srv.SaveSnapshot = func() error { return store.SaveSnapshot(*snapshotPath) }
	}

	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           srv.NewRouter(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// 周期落盘。
	var flushDone chan struct{}
	if *snapshotPath != "" && *flushInterval > 0 {
		flushDone = make(chan struct{})
		go func() {
			defer close(flushDone)
			t := time.NewTicker(*flushInterval)
			defer t.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-t.C:
					if err := store.SaveSnapshot(*snapshotPath); err != nil {
						log.Printf("periodic snapshot failed: %v", err)
					}
				}
			}
		}()
	}

	go func() {
		log.Printf("cardinalgov listening on %s (series-budget=%d snapshot=%q)", *addr, store.Config().DefaultSeriesBudget, *snapshotPath)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down ...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	if flushDone != nil {
		<-flushDone
	}
	if *snapshotPath != "" {
		if err := store.SaveSnapshot(*snapshotPath); err != nil {
			log.Printf("final snapshot failed: %v", err)
		} else {
			log.Printf("final snapshot saved to %s", *snapshotPath)
		}
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envIntOr(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envBoolOr(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return def
}

func envDurationOr(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
