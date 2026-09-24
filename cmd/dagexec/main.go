// Command dagexec runs the resumable DAG execution HTTP service.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"dagexec/internal/api"
	"dagexec/internal/dag"
	"dagexec/internal/scheduler"
	"dagexec/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	stateFile := flag.String("state", "./data/state.json", "path to the persisted state file")
	maxAttempts := flag.Int("max-attempts", 3, "default attempts per node activation")
	maxParallel := flag.Int("max-parallel", 4, "default node concurrency per DAG")
	retryBase := flag.Duration("retry-base-delay", 200*time.Millisecond, "first retry back-off (doubled thereafter)")
	retryMax := flag.Duration("retry-max-delay", 30*time.Second, "retry back-off cap")
	flag.Parse()

	logger := log.New(os.Stdout, "dagexec: ", log.LstdFlags|log.Lmicroseconds)

	st, err := store.NewFileStore(*stateFile)
	if err != nil {
		logger.Fatalf("load state: %v", err)
	}
	sched := scheduler.New(st, dag.DefaultRegistry(), scheduler.Options{
		DefaultMaxAttempts: *maxAttempts,
		DefaultMaxParallel: *maxParallel,
		RetryBaseDelay:     *retryBase,
		MaxRetryDelay:      *retryMax,
		Logger:             logger,
	})
	if err := sched.Start(); err != nil {
		logger.Fatalf("resume schedulers: %v", err)
	}
	logger.Printf("resumed state from %s", *stateFile)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(sched).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		logger.Printf("listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http server: %v", err)
		}
	}()

	<-stop
	logger.Printf("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("http shutdown: %v", err)
	}
	sched.Shutdown()
	logger.Printf("stopped")
}
