// Command snapd runs the account-state snapshot/prune HTTP service.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"github.com/example/snapprune/internal/api"
	"github.com/example/snapprune/internal/store"
)

func main() {
	var (
		addr      = flag.String("addr", "127.0.0.1:8080", "HTTP listen address")
		dbDir     = flag.String("db", "./data", "Pebble data directory")
		keep      = flag.Int("keep", 3, "snapshots to retain after pruning")
		readerTTL = flag.Duration("reader-ttl", 10*time.Second, "default reader lease timeout")
		interval  = flag.Uint64("snapshot-interval", 0, "auto-build a snapshot every N blocks (0 = manual only)")
	)
	flag.Parse()

	st, err := store.Open(*dbDir, store.Options{
		KeepSnapshots:    *keep,
		ReaderTTL:        *readerTTL,
		SnapshotInterval: *interval,
	})
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	srv := api.NewServer(st)
	log.Printf("snapd listening on %s (db=%s keep=%d reader-ttl=%s snapshot-interval=%d)",
		*addr, *dbDir, *keep, *readerTTL, *interval)
	log.Fatal(http.ListenAndServe(*addr, srv.Handler()))
}
