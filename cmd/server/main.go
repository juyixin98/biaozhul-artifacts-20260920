// Command sse-server runs the durable SSE event service.
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

	"sse-resume/internal/broker"
	"sse-resume/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dataDir := flag.String("data", "./data", "directory for the durable event log")
	maxEvents := flag.Int("max-events", 10000, "retained events bound (0 = unlimited)")
	queueSize := flag.Int("queue", 128, "per-subscriber live-event buffer; overflow drops the subscriber")
	heartbeat := flag.Duration("heartbeat", 15*time.Second, "SSE keep-alive interval")
	writeTimeout := flag.Duration("write-timeout", 5*time.Second, "per-frame write deadline for stalled peers")
	noSync := flag.Bool("nosync", false, "skip fsync per event (UNSAFE; demo/tests only)")
	flag.Parse()

	br, err := broker.Open(broker.Config{
		Dir:       *dataDir,
		MaxEvents: *maxEvents,
		QueueSize: *queueSize,
		NoSync:    *noSync,
	})
	if err != nil {
		log.Fatalf("open broker: %v", err)
	}
	defer func() {
		if err := br.Close(); err != nil {
			log.Printf("close broker: %v", err)
		}
	}()

	srv := &http.Server{
		Addr:         *addr,
		Handler:      server.New(br, server.WithHeartbeat(*heartbeat), server.WithWriteTimeout(*writeTimeout)).Handler(),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 0, // SSE streams are long-lived; deadlines are per-frame
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		log.Printf("sse-resume listening on %s (data=%s, max-events=%d, queue=%d, heartbeat=%s)",
			*addr, *dataDir, *maxEvents, *queueSize, *heartbeat)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down ...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
		_ = srv.Close()
	}
	log.Printf("stopped")
}
