// DAMS API server.
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

	"dams/internal/api"
	"dams/internal/platform/dbpool"
	"dams/internal/platform/migrate"
	"dams/internal/service/alerts"
	"dams/internal/service/auditchain"
	"dams/internal/service/detection"
	"dams/internal/service/ingest"
	"dams/internal/service/ruleadmin"
)

func main() {
	dsn := env("DAMS_DATABASE_URL", "postgres://dams:dams@localhost:5432/dams?sslmode=disable")
	addr := env("DAMS_HTTP_ADDR", ":8080")
	automigrate := env("DAMS_AUTOMIGRATE", "true") == "true"

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := dbpool.New(ctx, dsn)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()

	if automigrate {
		n, err := migrate.Up(ctx, pool)
		if err != nil {
			log.Fatalf("migrate: %v", err)
		}
		if n > 0 {
			log.Printf("applied %d migration(s)", n)
		}
	}

	engine := detection.New()
	chain := auditchain.New()
	deps := api.Deps{
		Pool:   pool,
		Ingest: ingest.New(pool, engine),
		Rules:  &ruleadmin.Service{Pool: pool, Chain: chain},
		Alerts: &alerts.Service{Pool: pool, Chain: chain, Engine: engine},
		Chain:  chain,
		Engine: engine,
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           api.NewRouter(deps),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("DAMS listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()
	<-ctx.Done()
	log.Println("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shCtx)
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
