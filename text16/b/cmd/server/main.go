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

	"github.com/example/forensiccore/internal/api"
	"github.com/example/forensiccore/internal/config"
	"github.com/example/forensiccore/internal/service"
	"github.com/example/forensiccore/internal/store"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	db, err := store.Open(cfg.DBDriver, cfg.DSN)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}

	svc, err := service.New(db, cfg)
	if err != nil {
		log.Fatalf("init service: %v", err)
	}
	if err := svc.Start(); err != nil {
		log.Fatalf("start workers: %v", err)
	}
	defer svc.Close()

	router := api.Router(svc)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           router,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("ForensicCore listening on %s (db=%s, roots=%v)",
			cfg.HTTPAddr, cfg.DBDriver, rootNames(cfg))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("shutting down ...")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
}

func rootNames(cfg *config.Config) []string {
	names := make([]string, 0, len(cfg.Roots))
	for n := range cfg.Roots {
		names = append(names, n)
	}
	return names
}
