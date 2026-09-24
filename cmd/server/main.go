// Command server runs the scaling-stable-window HTTP service backed by SQLite.
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

	"scaler/internal/clock"
	"scaler/internal/httpapi"
	"scaler/internal/store"
)

func main() {
	addr := flag.String("addr", ":8090", "HTTP listen address")
	dbPath := flag.String("db", "data/scaler.db", "SQLite database path (use :memory: for ephemeral)")
	hmacSecret := flag.String("hmac-secret", "", "HMAC-SHA256 secret for decision signatures (env SCALER_HMAC_SECRET takes precedence; dev default is clearly marked)")
	flag.Parse()

	secret := os.Getenv("SCALER_HMAC_SECRET")
	if secret == "" {
		secret = *hmacSecret
	}
	if secret == "" {
		secret = "dev-only-insecure-secret-change-me"
		log.Printf("WARNING: using built-in development HMAC secret; set SCALER_HMAC_SECRET in production")
	}

	if *dbPath != ":memory:" {
		if err := os.MkdirAll(dbDir(*dbPath), 0o755); err != nil {
			log.Fatalf("create db directory: %v", err)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, *dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	srv := &httpapi.Server{
		Store:  st,
		Clock:  clock.System{},
		Secret: []byte(secret),
		Log:    log.New(os.Stdout, "[scaler] ", log.LstdFlags),
	}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("scaling-stable-window listening on %s (db=%s)", *addr, *dbPath)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func dbDir(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' || p[i] == os.PathSeparator {
			return p[:i]
		}
	}
	return "."
}
