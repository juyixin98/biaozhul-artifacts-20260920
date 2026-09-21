package main

import (
	"context"
	"database/sql"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"forensiccore/internal/api"
	"forensiccore/internal/config"
	"forensiccore/internal/database"
	"forensiccore/internal/jobs"
	"forensiccore/internal/service"
)

func main() {
	logger := log.New(os.Stderr, "forensiccore: ", log.LstdFlags)

	cfg, err := config.Load()
	if err != nil {
		logger.Fatalf("configuration error: %v", err)
	}

	db, err := database.Open(cfg.DBDriver, cfg.DSN)
	if err != nil {
		logger.Fatalf("database open: %v", err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		logger.Fatalf("database handle: %v", err)
	}
	sqlDB.SetMaxOpenConns(25)
	sqlDB.SetConnMaxLifetime(time.Hour)
	if err := waitForDB(sqlDB, 30*time.Second); err != nil {
		logger.Fatalf("database not reachable: %v", err)
	}
	if err := database.Migrate(db); err != nil {
		logger.Fatalf("database migrate: %v", err)
	}

	svc := &service.Service{
		DB:           db,
		EvidenceRoot: cfg.EvidenceRoot,
		ChunkSize:    cfg.ChunkSize,
	}
	runner := jobs.NewRunner(db, cfg.EvidenceRoot, cfg.ChunkSize,
		time.Duration(cfg.PollIntervalMS)*time.Millisecond, nil, nil)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := runner.ResetStale(ctx); err != nil {
		logger.Printf("reset stale jobs: %v", err)
	}
	runner.Start(ctx)
	logger.Printf("verification runner started (chunk size %d bytes)", cfg.ChunkSize)

	srv := &api.Server{DB: db, Service: svc, Runner: runner, Cfg: cfg}
	httpServer := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.NewRouter(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s (evidence root %s)", cfg.Listen, cfg.EvidenceRoot)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(shutdownCtx)
	runner.Stop()
}

func waitForDB(db *sql.DB, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var lastErr error
	for time.Now().Before(deadline) {
		if err := db.Ping(); err == nil {
			return nil
		} else {
			lastErr = err
		}
		time.Sleep(time.Second)
	}
	return lastErr
}
