// Command forensiccore runs the local evidence processing HTTP backend.
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

	"forensiccore/internal/api"
	"forensiccore/internal/cases"
	"forensiccore/internal/chain"
	"forensiccore/internal/config"
	"forensiccore/internal/database"
	"forensiccore/internal/evidence"
	"forensiccore/internal/report"
	"forensiccore/internal/review"
	"forensiccore/internal/securefile"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}

	db, err := database.Open(cfg.DBDriver, cfg.DSN, os.Getenv("GORM_VERBOSE") == "1")
	if err != nil {
		log.Fatalf("database: %v", err)
	}

	events := chain.New(db)
	resolver := securefile.NewResolver(cfg.WhitelistRoots)
	caseSvc := cases.New(db, events)
	evidenceSvc := evidence.New(db, resolver, events, cfg.ChunkSize)
	reviewMgr := review.NewManager(db, evidenceSvc, resolver, events, cfg.ChunkSize)
	reportSvc := report.New(db, events)

	// Resume jobs interrupted by a previous crash/stop, verifying identity and
	// prefix before continuing any of them.
	recovered, err := reviewMgr.RecoverInterrupted(context.Background())
	if err != nil {
		log.Printf("warning: recovering interrupted reviews: %v", err)
	} else if recovered > 0 {
		log.Printf("resumed %d interrupted review job(s)", recovered)
	}

	handler := api.NewServer(cfg, api.Services{
		Cases:    caseSvc,
		Evidence: evidenceSvc,
		Review:   reviewMgr,
		Chain:    events,
		Report:   reportSvc,
	})

	srv := &http.Server{
		Addr:              cfg.HTTPListen,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("ForensicCore listening on %s (whitelist roots: %v)", cfg.HTTPListen, cfg.WhitelistRoots)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("shutting down: checkpointing active reviews ...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	reviewMgr.Shutdown(shutdownCtx)
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	log.Println("stopped")
}
