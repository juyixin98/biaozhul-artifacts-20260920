// Command tracestitch runs the distributed-trace assembly backend:
// HTTP span ingestion, trace/revision queries, timeout sweeper and local
// WAL+snapshot persistence.
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

	"tracestitch/internal/assembler"
	"tracestitch/internal/clock"
	"tracestitch/internal/server"
	"tracestitch/internal/storage"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "HTTP listen address")
	dataDir := flag.String("data-dir", ".", "base directory; WAL/snapshots live under <data-dir>/data")
	timeout := flag.Duration("trace-timeout", 30*time.Second, "incomplete trace timeout")
	skewTolerance := flag.Duration("skew-tolerance", time.Millisecond,
		"allowed parent/child wall-clock nesting tolerance before flagging skew")
	sweepInterval := flag.Duration("sweep-interval", 2*time.Second, "how often the timeout sweeper runs")
	flag.Parse()

	store, err := storage.Open(*dataDir)
	if err != nil {
		log.Fatalf("open storage: %v", err)
	}
	defer store.Close()

	// Replay the WAL before serving: every revision is rebuilt deterministically.
	recs, err := store.ReadWAL()
	if err != nil {
		log.Fatalf("read wal: %v", err)
	}
	asm := assembler.New(
		assembler.Config{Timeout: *timeout, ClockSkewTolerance: *skewTolerance},
		clock.System{}, store, log.Printf,
	)
	asm.Replay(recs)
	log.Printf("replayed %d WAL record(s)", len(recs))

	srv := server.New(asm)

	stopSweeper := make(chan struct{})
	go server.StartSweeper(asm, *sweepInterval, stopSweeper)

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("tracestitch listening on %s (timeout=%s, sweep=%s)", *addr, *timeout, *sweepInterval)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
	log.Println("shutting down")
	close(stopSweeper)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
}
