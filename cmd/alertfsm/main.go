// Command alertfsm starts the alert hysteresis state machine HTTP service.
//
// It is a self-contained backend sample: metrics are ingested over HTTP,
// evaluated against threshold rules by a virtual clock, persisted to a local
// JSON snapshot, and notification events are exposed for polling. No real
// monitoring platform or external database is required.
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

	"alertfsm/internal/api"
	"alertfsm/internal/engine"
	"alertfsm/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("ALERTFSM_ADDR", ":8080"), "HTTP listen address")
	dataDir := flag.String("data", envOr("ALERTFSM_DATA", "./data"), "directory for the local snapshot")
	baseClock := flag.Int64("base-clock-ms", 0, "virtual clock seed used only when the data dir has no snapshot (0 = fixed epoch)")
	flag.Parse()

	st, err := store.New(*dataDir)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	// Seed the virtual clock on a fresh database.
	if st.Clock() == 0 {
		seed := *baseClock
		if seed == 0 {
			seed = engine.DefaultBaseClockMS
		}
		st.SetClockFresh(seed)
	}

	eng := engine.New(st)
	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(eng, st).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("alertfsm listening on %s (data=%s, virtual clock=%d)", *addr, *dataDir, eng.Clock())
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Println("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	if err := st.Save(); err != nil {
		log.Printf("final save: %v", err)
	}
	fmt.Println("stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
