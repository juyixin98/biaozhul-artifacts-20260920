// Command dynpoold serves the dynamic-thread-pool library over a local HTTP
// API. It is intentionally dependency-free (stdlib only).
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

	"dynpool/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	history := flag.Int("event-history", 2048, "per-pool event history retained")
	flag.Parse()

	srv := server.New(*history)
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		log.Printf("dynpoold listening on http://%s", *addr)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-stop
	log.Println("shutting down HTTP server (drain up to 10s)")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
}
