// Command synapticgo is the SynapticGo API server: it connects to
// PostgreSQL, applies migrations, reconciles crash leftovers, then serves.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"synapticgo/internal/app"
	"synapticgo/internal/config"
	"synapticgo/internal/httpapi"
	"synapticgo/internal/store"
)

func main() {
	cfg := config.FromEnv()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.Open(ctx, cfg.DatabaseURL, 30)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer db.Close()

	if err := store.Migrate(ctx, db); err != nil {
		log.Fatalf("migrations: %v", err)
	}

	fs, err := app.NewFileStore(cfg.DataDir)
	if err != nil {
		log.Fatalf("file store: %v", err)
	}

	// Reconcile any crash leftovers before accepting traffic.
	if err := (&app.DatasetService{DB: db, FS: fs}).Recover(ctx); err != nil {
		log.Fatalf("recovery: %v", err)
	}

	e := httpapi.New(db, fs)
	go func() {
		if err := e.Start(cfg.Addr); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()
	log.Printf("synapticgo listening on %s (data dir %s)", cfg.Addr, cfg.DataDir)

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := e.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
