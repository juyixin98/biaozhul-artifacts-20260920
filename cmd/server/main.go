// rollout-api：渐进发布判定器 HTTP 服务。
package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"time"

	"rollout/internal/api"
	"rollout/internal/metrics"
	"rollout/internal/store"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	dbURL := env("DATABASE_URL", "postgres://rollout:rollout@localhost:5432/rollout?sslmode=disable")
	metricsURL := env("METRICS_URL", "http://localhost:18081")
	addr := env("LISTEN_ADDR", ":18082")
	migrationPath := env("MIGRATION_PATH", "migrations/0001_init.sql")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	st, err := store.Open(ctx, dbURL)
	if err != nil {
		fatal("connect postgres: %v", err)
	}
	defer st.Close()

	if os.Getenv("SKIP_MIGRATIONS") != "1" {
		if err := st.ApplyMigrations(context.Background(), migrationPath); err != nil {
			fatal("apply migrations: %v", err)
		}
		fmt.Fprintln(os.Stderr, "migrations applied")
	}

	srv := &api.Server{
		Store:   st,
		Metrics: metrics.New(metricsURL),
		Now:     time.Now,
	}
	httpServer := &http.Server{
		Addr:              addr,
		Handler:           api.NewRouter(srv),
		ReadHeaderTimeout: 5 * time.Second,
	}
	fmt.Fprintf(os.Stderr, "rollout-api listening on %s (metrics=%s)\n", addr, metricsURL)
	if err := httpServer.ListenAndServe(); err != nil {
		fatal("http server: %v", err)
	}
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}
