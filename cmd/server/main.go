package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"communitygov/internal/config"
	"communitygov/internal/database"
	"communitygov/internal/handlers"
	"communitygov/internal/services"
)

func main() {
	cfg := config.Load()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	poolCfg, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("parse database url: %v", err)
	}
	poolCfg.MaxConns = 20
	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()

	if err := waitForDB(ctx, pool); err != nil {
		log.Fatalf("db not reachable: %v", err)
	}
	if err := database.Migrate(ctx, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Println("migrations applied")

	store := database.NewStore(pool)
	userSvc := services.NewUserService(store)
	postSvc := services.NewPostService(store)
	memberSvc := services.NewMembershipService(store)
	reportSvc := services.NewReportService(store, postSvc)
	courseSvc := services.NewCourseService(store)

	h := &handlers.Handlers{
		Users: userSvc, Posts: postSvc, Memberships: memberSvc,
		Reports: reportSvc, Courses: courseSvc,
	}
	router := handlers.NewRouter(handlers.RouterDeps{
		Store: store, Handlers: h, JWTSecret: cfg.JWTSecret, JWTTTL: cfg.JWTTTL,
	})

	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: router,
		ReadHeaderTimeout: 10 * time.Second}
	go func() {
		log.Printf("listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("serve: %v", err)
		}
	}()
	<-ctx.Done()
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}

func waitForDB(ctx context.Context, pool *pgxpool.Pool) error {
	deadline := time.Now().Add(30 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		if err := pool.Ping(ctx); err == nil {
			return nil
		} else {
			lastErr = err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
	if lastErr == nil {
		lastErr = os.ErrDeadlineExceeded
	}
	return lastErr
}
