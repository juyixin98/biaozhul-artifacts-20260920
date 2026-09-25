// Command wsd runs the work-stealing executor as a local HTTP service.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"worksteal/commands"
	"worksteal/scheduler"
	"worksteal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	workers := flag.Int("workers", 0, "fixed worker count (default: NumCPU)")
	eventLogPath := flag.String("event-log", "", "optional path: append all events as JSON Lines")
	eventBuffer := flag.Int("event-buffer", 4096, "in-memory event ring buffer size")
	drainTimeout := flag.Duration("drain-timeout", 10*time.Second,
		"max wait for running tasks on graceful shutdown before contexts are canceled")
	flag.Parse()

	var extraSinks []scheduler.EventSink
	var eventFile *os.File
	if *eventLogPath != "" {
		f, err := os.OpenFile(*eventLogPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			log.Fatalf("open event log: %v", err)
		}
		eventFile = f
		extraSinks = append(extraSinks, scheduler.NewJSONSink(f))
	}
	srv, err := server.New(server.Config{
		Addr:        *addr,
		Workers:     *workers,
		EventBuffer: *eventBuffer,
		ExtraSinks:  extraSinks,
	})
	if err != nil {
		log.Fatalf("create server: %v", err)
	}
	commands.Register(srv.Exec)
	if eventFile != nil {
		defer eventFile.Close()
	}

	log.Printf("wsd listening on %s (workers=%d)", srv.Addr(), srv.Exec.NumWorkers())
	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		if err != nil {
			log.Fatalf("serve: %v", err)
		}
	case sig := <-sigCh:
		log.Printf("received %s, draining (timeout %s)...", sig, *drainTimeout)
		ctx, cancel := context.WithTimeout(context.Background(), *drainTimeout)
		defer cancel()
		if err := srv.Close(ctx, *drainTimeout); err != nil {
			log.Printf("shutdown: %v", err)
		}
		log.Printf("shutdown complete")
	}
}
