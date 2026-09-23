// Command dynpoold runs the dynamic thread-pool HTTP service.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"dynpool/pool"
	"dynpool/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	workers := flag.Int("workers", 4, "initial worker count")
	queue := flag.Int("queue", 128, "bounded queue capacity")
	policyName := flag.String("policy", "abort",
		"rejection policy: abort|caller_runs|discard|discard_oldest")
	name := flag.String("name", "default", "pool name (used in events)")
	flag.Parse()

	policy, err := parsePolicy(*policyName)
	if err != nil {
		log.Fatalf("bad -policy: %v", err)
	}

	srv, err := server.New(server.Options{
		Name:          *name,
		Workers:       *workers,
		QueueCapacity: *queue,
		Policy:        policy,
	})
	if err != nil {
		log.Fatalf("create pool: %v", err)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		log.Printf("dynpoold listening on %s (workers=%d queue=%d policy=%s)",
			*addr, *workers, *queue, *policyName)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	<-stop
	log.Println("shutdown signal received; draining pool gracefully")

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Pool().Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown incomplete: %v (force-canceling)", err)
		if _, ferr := srv.Pool().ShutdownNow(ctx); ferr != nil {
			log.Printf("force shutdown: %v", ferr)
		}
	}
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer shutCancel()
	_ = httpSrv.Shutdown(shutCtx)
	log.Println("terminated")
}

func parsePolicy(s string) (pool.RejectPolicy, error) {
	switch s {
	case "abort", "":
		return pool.RejectAbort, nil
	case "caller_runs":
		return pool.RejectCallerRuns, nil
	case "discard":
		return pool.RejectDiscard, nil
	case "discard_oldest":
		return pool.RejectDiscardOldest, nil
	default:
		return pool.RejectAbort, errBadPolicy
	}
}

var errBadPolicy = &policyError{}

type policyError struct{}

func (*policyError) Error() string { return "unknown rejection policy" }
