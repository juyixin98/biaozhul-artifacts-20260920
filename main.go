// Command counterreset runs the time-series counter backend: HTTP ingestion
// and querying with a local WAL-backed store and synthetic seed data.
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

	"counterreset/api"
	"counterreset/seed"
	"counterreset/store"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dataDir := flag.String("data", "data", "directory for the WAL")
	seedFlag := flag.Bool("seed", true, "seed synthetic data on startup if the store is empty")
	flag.Parse()

	st, err := store.Open(*dataDir)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}

	if *seedFlag && len(st.List()) == 0 {
		if err := seedStore(st); err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Printf("synthetic seed data loaded")
	} else {
		log.Printf("seeding skipped (store has %d series or seed disabled)", len(st.List()))
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.New(st),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	if err := st.Close(); err != nil {
		log.Printf("store close: %v", err)
	}
}

func seedStore(st *store.Store) error {
	for _, b := range seed.All() {
		if _, _, err := st.Ingest(b.Metric, b.Labels, b.Samples); err != nil {
			return err
		}
	}
	return nil
}
