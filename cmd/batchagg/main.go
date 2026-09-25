// Command batchagg runs the batch aggregation scheduler with the local HTTP
// interface and a simulated inference backend.
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"batchagg"
	"batchagg/httpapi"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "HTTP listen address")
	maxItems := flag.Int("max-items", 8, "flush batch when it reaches this many items")
	maxBytes := flag.Int("max-bytes", 64*1024, "flush batch when payload bytes reach this size; larger single items are rejected")
	maxWait := flag.Duration("max-wait", 50*time.Millisecond, "max wait since batch open before flush")
	eventLog := flag.String("event-log", "", "if set, append structured events as JSON lines to this file ('-' for stdout)")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	// Build the SSE broadcaster first so the scheduler can emit into it.
	broadcaster := httpapi.NewEventBroadcaster(256)
	sinks := []batchagg.EventSink{
		batchagg.NewSlogSink(logger),
		broadcaster, // live GET /events stream
	}
	switch *eventLog {
	case "":
	case "-":
		sinks = append(sinks, batchagg.NewJSONLinesSink(os.Stdout))
	default:
		f, err := os.OpenFile(*eventLog, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			logger.Error("cannot open event log", "path", *eventLog, "err", err)
			os.Exit(1)
		}
		defer f.Close()
		sinks = append(sinks, batchagg.NewJSONLinesSink(f))
	}

	sched := batchagg.New(batchagg.Config{
		MaxItems: *maxItems,
		MaxBytes: *maxBytes,
		MaxWait:  *maxWait,
		Clock:    batchagg.NewSystemClock(),
		Sink:     batchagg.MultiSink(sinks...),
	}, &batchagg.Simulator{DefaultModel: "sim-llm-1"})

	srv := httpapi.NewServer(httpapi.Config{
		Scheduler:    sched,
		Logger:       logger,
		MaxBodyBytes: *maxBytes + 4096,
		Broadcaster:  broadcaster,
	})

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	serverErr := make(chan error, 1)
	go func() {
		logger.Info("batch aggregation scheduler listening",
			"addr", *addr, "max_items", *maxItems, "max_bytes", *maxBytes, "max_wait", maxWait.String())
		if err := srv.ListenAndServe(*addr); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()

	select {
	case err := <-serverErr:
		logger.Error("http server failed", "err", err)
		os.Exit(1)
	case <-ctx.Done():
		logger.Info("shutdown signal received")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// Shutdown closes the listener immediately (no new connections) but waits
	// for active ones. /infer handlers are parked on their item result, so run
	// the scheduler drain concurrently: it flushes pending batches (reason
	// "shutdown"), those parked handlers return, and HTTP shutdown completes.
	httpDone := make(chan struct{})
	go func() {
		defer close(httpDone)
		if err := srv.Shutdown(shutdownCtx); err != nil {
			logger.Error("http shutdown error", "err", err)
		}
	}()
	if err := sched.Close(); err != nil {
		logger.Error("scheduler close error", "err", err)
	}
	<-httpDone
	logger.Info("shutdown complete")
}
