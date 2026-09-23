// Command server runs the multiline log ingestion and query HTTP
// service with local JSONL persistence.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"syscall"
	"time"

	"logpipe/internal/server"
	"logpipe/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dataDir := flag.String("data-dir", "./data", "directory for entries.jsonl")
	startPattern := flag.String("start-pattern", `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}`,
		"regular expression matching lines that start a multiline entry")
	timeout := flag.Duration("timeout", 5*time.Second,
		"idle time after which a pending entry is flushed")
	maxBytes := flag.Int("max-bytes", 4096,
		"maximum size in bytes of an assembled entry")
	sweep := flag.Duration("sweep-interval", 250*time.Millisecond,
		"how often the timeout sweeper runs")
	shutdownTimeout := flag.Duration("shutdown-timeout", 5*time.Second,
		"maximum time to wait for in-flight HTTP requests on shutdown")
	flag.Parse()

	rule, err := regexp.Compile(*startPattern)
	if err != nil {
		log.Fatalf("invalid start pattern: %v", err)
	}

	logger := log.New(os.Stdout, "logpipe: ", log.LstdFlags)

	st, err := store.Open(*dataDir)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	logger.Printf("store ready at %s (%d entries replayed)", *dataDir, st.Count())

	srv := server.New(server.Config{
		StartRule:     rule,
		Timeout:       *timeout,
		MaxBytes:      *maxBytes,
		SweepInterval: *sweep,
	}, st, logger)

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		logger.Printf("listening on %s (start rule %q, timeout %s, max-bytes %d)",
			*addr, *startPattern, *timeout, *maxBytes)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Printf("shutdown signal received, draining HTTP traffic")
	shutCtx, cancel := context.WithTimeout(context.Background(), *shutdownTimeout)
	defer cancel()
	if err := httpSrv.Shutdown(shutCtx); err != nil {
		logger.Printf("http shutdown: %v", err)
	}
	// Merge-flush remaining pending entries (marked "shutdown"), then
	// close the persisted log.
	srv.Close()
	if err := st.Close(); err != nil {
		logger.Printf("close store: %v", err)
	}
	logger.Printf("stopped cleanly")
}
