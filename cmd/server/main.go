// Command server runs the SIRCC incident-response API. It applies embedded
// migrations at startup, then serves HTTP and runs the reminder scheduler.
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

	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/httpapi"
	"sircc/internal/migrate"
	"sircc/internal/reminder"
)

func main() {
	dsn := getenv("DATABASE_URL", "postgres://sircc:sircc@127.0.0.1:5432/sircc?sslmode=disable")
	addr := getenv("ADDR", ":8080")
	reminderInterval := 10 * time.Second
	if v := os.Getenv("REMINDER_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			log.Fatalf("invalid REMINDER_INTERVAL %q: %v", v, err)
		}
		reminderInterval = d
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		log.Fatalf("connect pool: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping database: %v", err)
	}
	if err := migrate.Apply(ctx, pool); err != nil {
		log.Fatalf("apply migrations: %v", err)
	}

	scheduler := reminder.NewScheduler(pool, reminderInterval)
	go scheduler.Run(ctx)

	srv := &http.Server{
		Addr:              addr,
		Handler:           httpapi.NewServer(pool).Router(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()

	log.Printf("sircc listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("http server: %v", err)
	}
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
