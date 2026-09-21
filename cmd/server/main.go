package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/jmoiron/sqlx"

	"synapticgo/internal/config"
	"synapticgo/internal/dataset"
	"synapticgo/internal/db"
	"synapticgo/internal/experiments"
	"synapticgo/internal/httpx"
	"synapticgo/internal/modelx"
	"synapticgo/internal/store"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	dbx, err := sqlx.Connect("pgx", cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	dbx.SetMaxOpenConns(20)
	dbx.SetMaxIdleConns(10)
	dbx.SetConnMaxLifetime(time.Hour)

	if err := db.Migrate(dbx); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Printf("migrations applied")

	fs, err := store.New(cfg.DataDir)
	if err != nil {
		log.Fatalf("file store: %v", err)
	}
	if err := store.Recover(dbx, fs); err != nil {
		log.Fatalf("startup recovery: %v", err)
	}
	log.Printf("object store recovered at %s", cfg.DataDir)

	objSvc := store.NewObjectService(dbx, fs)
	deps := httpx.Deps{
		DB:           dbx,
		Datasets:     dataset.NewService(dbx, objSvc, fs),
		Models:       modelx.NewService(dbx),
		Experiments:  experiments.NewService(dbx),
		MaxBodyBytes: cfg.MaxBodyBytes,
	}

	e := httpx.NewRouter(deps)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           e,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("SynapticGo listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown: %v", err)
	}
	_ = dbx.Close()
}
