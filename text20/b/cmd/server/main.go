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

	"signalboard/internal/config"
	httpapi "signalboard/internal/httpapi"
	"signalboard/internal/migrate"
	"signalboard/internal/seed"
	"signalboard/internal/service"
)

func main() {
	cfg := config.Load()
	apiKey := os.Getenv("MANAGEMENT_API_KEY")
	if apiKey == "" {
		apiKey = "dev-management-key"
		log.Print("WARNING: using default MANAGEMENT_API_KEY; set the env var in production")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()

	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping db: %v", err)
	}

	// Apply migrations before anything else (idempotent).
	conn, err := pool.Acquire(ctx)
	if err != nil {
		log.Fatalf("acquire: %v", err)
	}
	if err := migrate.Up(ctx, conn.Conn()); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	conn.Release()
	log.Print("migrations applied")

	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		return
	}

	if len(os.Args) > 1 && os.Args[1] == "seed" {
		res, err := seed.Run(ctx, pool)
		if err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Printf("seeded store=%d published_version=%d screen_id=%d screen_token=%s",
			res.StoreID, res.PublishedVersion, res.ScreenID, res.ScreenToken)
		return
	}

	svc := service.New(pool)
	srv := httpapi.NewServer(svc, apiKey)

	httpSrv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("SignalBoard listening on %s", cfg.Addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Print("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
