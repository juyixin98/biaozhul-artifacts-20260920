// Command rsvrvd runs the local HTTP API for the reservation scheduler.
// It is an in-memory, single-process server: restarting it loses all state.
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

	"rsrv/sched"
	"rsrv/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	eventFile := flag.String("events", "", "optional path: append structured events as JSONL")
	flag.Parse()

	var sinks []sched.Sink
	memLog := sched.NewEventLog()
	sinks = append(sinks, memLog)
	if *eventFile != "" {
		jl, err := sched.NewJSONLSink(*eventFile)
		if err != nil {
			log.Fatalf("open event log %s: %v", *eventFile, err)
		}
		defer jl.Close()
		sinks = append(sinks, jl)
	}

	sch := sched.New(sched.WithSink(sched.MultiSink(sinks...)))
	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(sch).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("reservation scheduler listening on http://%s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("serve: %v", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	log.Println("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shCtx); err != nil {
		fmt.Fprintln(os.Stderr, "shutdown error:", err)
	}
}
