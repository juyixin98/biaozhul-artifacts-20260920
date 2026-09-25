// Command server runs the cardinality-budget observability backend: HTTP
// ingest/query with local JSON-snapshot persistence.
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
	"strconv"
	"syscall"
	"time"

	"cardinalitybudget/internal/api"
	"cardinalitybudget/internal/persist"
	"cardinalitybudget/internal/store"
)

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
		log.Printf("ignoring bad int env %s=%q", key, v)
	}
	return def
}

func envString(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	var (
		addr             = flag.String("addr", envString("CB_ADDR", ":8080"), "HTTP listen address")
		dataDir          = flag.String("data-dir", envString("CB_DATA_DIR", "./data"), "snapshot directory (empty disables persistence)")
		flushInterval    = flag.Duration("flush-interval", envDuration("CB_FLUSH_INTERVAL", 5*time.Second), "periodic snapshot interval; 0 disables")
		seriesBudget     = flag.Int("max-series", envInt("CB_MAX_SERIES", 1000), "per-metric label-combination budget")
		metricBudget     = flag.Int("max-metrics", envInt("CB_MAX_METRICS", 1000), "distinct metric-name budget")
		maxMetricNameLen = flag.Int("max-metric-name-len", envInt("CB_MAX_METRIC_NAME_LEN", 256), "max metric name bytes")
		maxLabelKeys     = flag.Int("max-label-keys", envInt("CB_MAX_LABEL_KEYS", 20), "max labels per sample")
		maxLabelKeyLen   = flag.Int("max-label-key-len", envInt("CB_MAX_LABEL_KEY_LEN", 128), "max label-key bytes")
		maxLabelValueLen = flag.Int("max-label-value-len", envInt("CB_MAX_LABEL_VALUE_LEN", 512), "max label-value bytes (truncated)")
		ignorePersistCfg = flag.Bool("ignore-persist-config", false, "accept snapshot even if its budget config differs (not recommended)")
	)
	flag.Parse()

	logger := log.New(os.Stdout, "cardinality-budget: ", log.LstdFlags|log.Lmsgprefix)

	cfg := store.Config{
		MaxSeriesPerMetric: *seriesBudget,
		MaxMetricNames:     *metricBudget,
		MaxMetricNameLen:   *maxMetricNameLen,
		MaxLabelKeys:       *maxLabelKeys,
		MaxLabelKeyLen:     *maxLabelKeyLen,
		MaxLabelValueLen:   *maxLabelValueLen,
	}
	if err := cfg.Validate(); err != nil {
		logger.Fatalf("invalid config: %v", err)
	}

	var pstore *persist.Store
	if *dataDir != "" {
		ps, err := persist.New(*dataDir)
		if err != nil {
			logger.Fatalf("persistence init: %v", err)
		}
		pstore = ps
	}

	st, err := loadOrCreate(pstore, cfg, *ignorePersistCfg, logger)
	if err != nil {
		logger.Fatalf("startup: %v", err)
	}

	var flusher api.Flusher
	if pstore != nil {
		f := &flusherHandle{store: st, pstore: pstore, logger: logger}
		flusher = f
		defer func() {
			if err := f.Flush(); err != nil {
				logger.Printf("final flush failed: %v", err)
			}
		}()
	}

	srv := api.NewServer(st, logger, flusher)
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	rootCtx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	if pstore != nil && *flushInterval > 0 {
		go flushLoop(rootCtx, st, pstore, *flushInterval, logger)
	}

	go func() {
		logger.Printf("listening on %s (data dir %q)", *addr, *dataDir)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http server: %v", err)
		}
	}()

	<-rootCtx.Done()
	logger.Printf("shutdown signal received, draining")
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutCancel()
	if err := httpSrv.Shutdown(shutCtx); err != nil {
		logger.Printf("http shutdown: %v", err)
	}
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
		log.Printf("ignoring bad duration env %s=%q", key, v)
	}
	return def
}

func loadOrCreate(pstore *persist.Store, cfg store.Config, ignoreCfg bool, logger *log.Logger) (*store.Store, error) {
	if pstore == nil {
		return store.New(cfg)
	}
	exists, err := pstore.Exists()
	if err != nil {
		return nil, err
	}
	if !exists {
		logger.Printf("no snapshot at %s, starting empty", pstore.Path())
		return store.New(cfg)
	}
	snap, err := pstore.Load()
	if err != nil {
		return nil, fmt.Errorf("load snapshot: %w", err)
	}
	want := &cfg
	if ignoreCfg {
		want = nil
	}
	st, err := store.Restore(snap, want)
	if err != nil {
		return nil, fmt.Errorf("restore snapshot: %w (use -ignore-persist-config to start empty instead? no: delete the file)", err)
	}
	logger.Printf("restored from %s: %s", pstore.Path(), summarize(st))
	return st, nil
}

func summarize(st *store.Store) string {
	b, _ := json.Marshal(st.Stats())
	return string(b)
}

type flusherHandle struct {
	store  *store.Store
	pstore *persist.Store
	logger *log.Logger
}

func (f *flusherHandle) Flush() error {
	snap := f.store.Export()
	if err := f.pstore.Save(snap); err != nil {
		return err
	}
	return nil
}

func flushLoop(ctx context.Context, st *store.Store, pstore *persist.Store, interval time.Duration, logger *log.Logger) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := pstore.Save(st.Export()); err != nil {
				logger.Printf("periodic flush failed: %v", err)
			}
		}
	}
}
