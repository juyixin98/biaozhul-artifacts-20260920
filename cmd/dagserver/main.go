// Command dagserver runs the resumable DAG executor as an HTTP service.
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

	"dagexec/internal/engine"
	"dagexec/internal/server"
	"dagexec/internal/store"
	"dagexec/internal/task"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dataDir := flag.String("data", "./data", "directory for snapshot files")
	flag.Parse()

	st, err := store.NewFileStore(*dataDir)
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	eng := engine.New(st, task.Builtins())

	rootCtx, stopSignal := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stopSignal()

	if err := eng.Start(rootCtx); err != nil {
		log.Fatalf("engine start: %v", err)
	}
	log.Printf("resumed snapshots from %s", *dataDir)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(eng).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		log.Printf("listening on %s (whitelist: %v)", *addr, task.Builtins().Names())
		serverErr <- srv.ListenAndServe()
	}()

	select {
	case <-rootCtx.Done():
		log.Printf("shutdown signal received")
	case err := <-serverErr:
		if err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}

	// Stop accepting new requests, then stop schedulers (in-flight tasks are
	// interrupted and rolled back to pending on disk).
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	if err := eng.Shutdown(shutdownCtx); err != nil {
		log.Printf("engine shutdown: %v", err)
	}
	log.Printf("stopped cleanly")
	_ = os.Stdout.Sync()
}
