// Command server runs the chunked result streaming HTTP service.
// All upstream data comes from the in-process fake service; nothing
// talks to any production system.
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

	"streamback/internal/clock"
	"streamback/internal/server"
	"streamback/internal/upstream"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	bufferSlots := flag.Int("buffer-slots", 4, "max items buffered per request")
	bufferBytes := flag.Int("buffer-bytes", 256*1024, "max payload bytes buffered per request")
	maxItemBytes := flag.Int("max-item-bytes", 128*1024, "max single item size; larger requests are rejected with 413")
	shutdownGrace := flag.Duration("shutdown-grace", 5*time.Second, "grace period for in-flight streams on shutdown")
	flag.Parse()

	cfg := server.Config{
		BufferSlots:  *bufferSlots,
		BufferBytes:  *bufferBytes,
		MaxItemBytes: *maxItemBytes,
		MaxLineBytes: 1024 * 1024,
	}
	srv, err := server.New(cfg, upstream.New(clock.Real{}))
	if err != nil {
		log.Fatalf("invalid config: %v", err)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		log.Printf("listening on http://%s (bufferSlots=%d bufferBytes=%d maxItemBytes=%d)",
			*addr, cfg.BufferSlots, cfg.BufferBytes, cfg.MaxItemBytes)
		errCh <- httpSrv.ListenAndServe()
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	select {
	case s := <-sig:
		log.Printf("received %s, shutting down", s)
	case err := <-errCh:
		if err != nil && err != http.ErrServerClosed {
			log.Fatalf("serve: %v", err)
		}
		return
	}

	// 1. Tell in-flight streams to finish with a SERVER_SHUTDOWN trailer.
	if err := srv.Shutdown(*shutdownGrace); err != nil {
		log.Printf("stream shutdown: %v", err)
	}
	// 2. Stop accepting connections and wait for handlers to return.
	ctx, cancel := context.WithTimeout(context.Background(), *shutdownGrace)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	log.Println("shutdown complete")
}
