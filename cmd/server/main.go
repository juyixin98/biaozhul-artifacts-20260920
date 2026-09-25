// Command server runs the hysteresis alert engine with an HTTP API and
// local JSON snapshot persistence. All evaluation is driven by the virtual
// clock advanced through /clock/tick or sample timestamps — never by wall
// time.
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

	"github.com/example/hysteresis-alerter/internal/engine"
	"github.com/example/hysteresis-alerter/internal/httpapi"
	"github.com/example/hysteresis-alerter/internal/persist"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "HTTP listen address")
	dbPath := flag.String("db", "./data/snapshot.json", "path to the JSON snapshot file")
	flag.Parse()

	eng := engine.NewEngine()

	store, err := persist.New(*dbPath)
	if err != nil {
		log.Fatalf("persist init: %v", err)
	}
	if snap, err := store.Load(); err != nil {
		log.Fatalf("load snapshot from %s: %v", store.Path(), err)
	} else if snap != nil {
		if err := eng.Restore(snap); err != nil {
			log.Fatalf("restore snapshot: %v", err)
		}
		log.Printf("restored state from %s: clock_now=%s rules=%d events=%d",
			store.Path(), eng.Now().UTC().Format(time.RFC3339Nano),
			len(snap.Rules), len(snap.Events))
	} else {
		log.Printf("no snapshot at %s, starting fresh at virtual clock %s",
			store.Path(), eng.Now().UTC().Format(time.RFC3339Nano))
	}

	// Persist after every state-changing call. The file store writes
	// atomically; local writes are fast enough for the demo workload.
	eng.WithPersist(func(snap *engine.Snapshot) {
		if err := store.Save(snap); err != nil {
			log.Printf("snapshot save failed: %v", err)
		}
	})

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.New(eng),
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Periodic save is belt-and-braces: the persist hook already saves on
	// each mutation, but this guarantees a fresh file even for long idle
	// periods.
	stopTicker := make(chan struct{})
	go func() {
		t := time.NewTicker(30 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-t.C:
				if err := store.Save(eng.Export()); err != nil {
					log.Printf("periodic snapshot save failed: %v", err)
				}
			case <-stopTicker:
				return
			}
		}
	}()

	go func() {
		log.Printf("hysteresis alerter listening on http://%s", *addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigCh
	log.Printf("received %s, shutting down and saving snapshot ...", sig)
	close(stopTicker)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	if err := store.Save(eng.Export()); err != nil {
		log.Printf("final snapshot save failed: %v", err)
	} else {
		log.Printf("snapshot saved to %s", store.Path())
	}
}
