// Command merklekv runs one replica of the anti-entropy key/value service.
//
// Two copies of it on different --listen addresses form a pair; trigger
// reconciliation either by calling POST /v1/sync on one replica (demo /
// curl) or with the bundled client package (tests).
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

	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/server"
	"github.com/example/merklekv/internal/store"
)

func main() {
	var (
		listen      = flag.String("listen", "127.0.0.1:8080", "address to listen on")
		replicaID   = flag.String("replica-id", "r1", "stable id of this replica (used for LWW tie-breaking)")
		fanout      = flag.Int("fanout", 16, "Merkle tree fanout (2..256)")
		depth       = flag.Int("depth", 3, "Merkle tree depth (1..10); buckets = fanout^depth")
		snapshotTTL = flag.Duration("snapshot-ttl", 30*time.Second, "how long pinned snapshots are retained")
	)
	flag.Parse()

	params, err := merkle.NewParams(*fanout, *depth)
	if err != nil {
		log.Fatalf("bad Merkle parameters: %v", err)
	}

	logger := log.New(os.Stdout, "["+*replicaID+"] ", log.LstdFlags|log.Lmsgprefix)
	st := store.New(store.Config{
		ReplicaID:   *replicaID,
		SnapshotTTL: *snapshotTTL,
	})
	srv := server.New(st, params, logger)

	httpSrv := &http.Server{
		Addr:              *listen,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s (fanout=%d depth=%d buckets=%d)",
			*listen, params.Fanout, params.Depth, params.NumBuckets())
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	logger.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
