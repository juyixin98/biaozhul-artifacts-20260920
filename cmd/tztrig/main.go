// Command tztrig is a timezone-aware cron-style trigger service.
// Pure backend (net/http); see README.md for the HTTP API and examples.
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

	"tztrig/internal/api"
	"tztrig/internal/engine"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	statePath := flag.String("state", "data/state.json", "snapshot file for persistence (empty disables it)")
	catchUp := flag.Int("catchup", 100, "max missed firings delivered per schedule after downtime")
	flag.Parse()

	log.SetFlags(log.LstdFlags | log.LUTC)
	log.Printf("tztrig starting on %s (catch-up limit: %d, state: %q)", *addr, *catchUp, *statePath)

	var store engine.Store
	var preloaded *engine.Snapshot
	if *statePath != "" {
		fs := engine.NewFileStore(*statePath)
		store = fs
		snap, err := engine.LoadFile(*statePath)
		switch {
		case err != nil:
			log.Printf("WARNING: cannot load state file, starting empty: %v", err)
		case snap != nil:
			preloaded = snap
		}
	}

	eng := engine.New(engine.RealClock{},
		engine.WithCatchUpLimit(*catchUp),
		engine.WithStore(store),
		engine.WithFireCallback(func(f engine.Fire) {
			log.Printf("FIRE id=%s schedule=%s event=%s reason=%s",
				f.ID, f.ScheduleID, f.EventTime.Format(time.RFC3339), f.Reason)
		}),
	)
	if preloaded != nil {
		eng.Restore(preloaded)
		log.Printf("restored snapshot: %d schedule(s), %d fired id(s), tzdata %s",
			len(eng.List()), len(preloaded.Fired), preloaded.TZData)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.New(eng),
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Deterministic scheduler loop: sleep only until the next firing, never
	// poll. Catch-up on restart is handled by the initial Advance below.
	go runLoop(ctx, eng)

	// Deliver anything missed while the process was down (bounded by the
	// catch-up limit) before we start serving traffic.
	eng.Advance(engine.RealClock{}.Now())

	go func() {
		log.Printf("listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutdown signal received, draining HTTP")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
	// Final progression + persistence happens via store on each Advance;
	// snapshot one last time to capture lastFired precisely.
	if *statePath != "" {
		_ = store.Save(eng.Snapshot())
	}
	log.Println("stopped")
}

// runLoop sleeps until the next scheduled instant, fires, then repeats. If no
// schedule exists it re-evaluates every 30 seconds.
func runLoop(ctx context.Context, eng *engine.Engine) {
	for {
		next, id, ok := eng.NextWakeup()
		wait := 30 * time.Second
		if ok {
			d := time.Until(next)
			if d < wait {
				wait = d
			}
			log.Printf("next firing: schedule=%s at %s (in %s)", id,
				next.Format(time.RFC3339), d.Round(time.Second))
		}
		if wait < 0 {
			wait = 0
		}
		t := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-t.C:
		}
		// Process with a 1-second look-ahead so an instant reached while
		// waking is included; idempotent IDs prevent double execution.
		eng.Advance(engine.RealClock{}.Now().Add(time.Second))
	}
}
