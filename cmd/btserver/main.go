// Command btserver runs the resumable behavior-tree HTTP backend.
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

	"resumable-bt/internal/engine"
	"resumable-bt/internal/httpapi"
	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
)

func main() {
	addr := envOr("BT_HTTP_ADDR", ":8080")
	dsn := envOr("BT_DATABASE_DSN",
		"postgres://btapp:btapp_dev_pw@localhost:5432/btdb?sslmode=disable")

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, dsn)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer st.Close()
	if err := st.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Printf("schema migrated")

	reg := stub.NewRegistry()
	eng := engine.New(st, reg)
	defer eng.Close()

	if err := eng.Recover(ctx); err != nil {
		log.Printf("recovery: %v", err)
	} else {
		log.Printf("startup recovery complete")
	}

	srv := &http.Server{
		Addr:              addr,
		Handler:           httpapi.New(eng).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
