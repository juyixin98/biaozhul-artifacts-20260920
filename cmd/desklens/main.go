package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"desklens/internal/admin"
	"desklens/internal/api"
	"desklens/internal/config"
	"desklens/internal/db"
	"desklens/internal/ingest"
	"desklens/internal/manager"
	"desklens/internal/store"
)

func main() {
	cfg := config.Load()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	database, err := db.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer database.Close()

	if cfg.RunMigrations {
		if err := db.Migrate(ctx, database); err != nil {
			log.Fatalf("migrations: %v", err)
		}
		log.Print("migrations applied")
	}
	if cfg.RunSeed {
		if err := db.Seed(ctx, database); err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Print("reference data seeded")
	}

	st := store.New(database)
	router := api.NewRouter(api.Handlers{
		Ingest:  ingest.New(database, st),
		Manager: manager.New(database),
		Admin:   admin.New(database),
	}, cfg.AdminAPIKey)

	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: router}
	go func() {
		log.Printf("DeskLens listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Print("shutting down")
	shutdownCtx, c := context.WithTimeout(context.Background(), 10*time.Second)
	defer c()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		os.Exit(1)
	}
}
