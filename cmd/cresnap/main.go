// Command cresnap is the cgroup v2 offline sample analysis HTTP service.
//
// It reads a directory of container samples, parses a strict cgroup v2 file
// subset, computes finite-difference rates, classifies resource events and
// persists results to SQLite.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"cresnap/internal/api"
	"cresnap/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("CRESNAP_ADDR", ":8080"), "listen address (env CRESNAP_ADDR)")
	fixtureRoot := flag.String("fixtures", envOr("CRESNAP_FIXTURES", "./fixtures"), "fixture root directory (env CRESNAP_FIXTURES)")
	dbPath := flag.String("db", envOr("CRESNAP_DB", "./data/cresnap.db"), "SQLite database path (env CRESNAP_DB)")
	flag.Parse()

	if st, err := os.Stat(*fixtureRoot); err != nil || !st.IsDir() {
		fmt.Fprintf(os.Stderr, "fixtures directory %q is not a readable directory: %v\n", *fixtureRoot, err)
		os.Exit(2)
	}
	if err := os.MkdirAll(dbDir(*dbPath), 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "cannot create database directory: %v\n", err)
		os.Exit(2)
	}

	db, err := store.Open(*dbPath + "?_pragma=busy_timeout(5000)")
	if err != nil {
		log.Fatalf("open sqlite %q: %v", *dbPath, err)
	}
	defer db.Close()

	srv := &api.Server{Root: *fixtureRoot, Store: db}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.NewRouter(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		log.Printf("cresnap listening on %s (fixtures=%s db=%s)", *addr, *fixtureRoot, *dbPath)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-stop
	log.Printf("shutdown signal received")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func dbDir(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' || p[i] == os.PathSeparator {
			return p[:i]
		}
	}
	return "."
}
