// Command testmerge runs the local test-result merging service.
//
// It is a backend-only JSON HTTP service. It never connects to any cloud
// platform: the event cache (-cache-dir) and fixture working directories
// (-work-dir) are plain local directories and are kept separate.
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

	"github.com/local/testmerge/internal/api"
	"github.com/local/testmerge/internal/executor"
	"github.com/local/testmerge/internal/store"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "testmerge:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	fs := flag.NewFlagSet("testmerge", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8099", "listen address (loopback by default; no auth)")
	cacheDir := fs.String("cache-dir", "./.testmerge-cache", "directory for durable event logs")
	workDir := fs.String("work-dir", "./.testmerge-work", "directory for fixture working dirs (kept separate from cache)")
	fixtures := fs.String("fixtures-root", "./testdata/fixtures", "directory containing fixture executables users may run")
	readTimeout := fs.Duration("read-timeout", 15*time.Second, "HTTP read timeout")
	if err := fs.Parse(args); err != nil {
		return err
	}

	st, err := store.Open(*cacheDir)
	if err != nil {
		return fmt.Errorf("open store: %w", err)
	}
	ex, err := executor.New(*fixtures, *workDir)
	if err != nil {
		return fmt.Errorf("init executor: %w", err)
	}

	srv := &api.Server{Store: st, Executor: ex}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           logRequests(srv.NewRouter()),
		ReadHeaderTimeout: *readTimeout,
	}

	go func() {
		log.Printf("testmerge listening on %s", *addr)
		log.Printf("  cache dir : %s", st.Root())
		log.Printf("  work dir  : %s", ex.WorkRoot())
		log.Printf("  fixtures  : %s", ex.FixturesRoot())
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	log.Println("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return httpSrv.Shutdown(shCtx)
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		h.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rw.status, time.Since(start).Round(time.Millisecond))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
