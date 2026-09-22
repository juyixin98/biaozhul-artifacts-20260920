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

	"activityguard/internal/api"
	"activityguard/internal/config"
	"activityguard/internal/db"
	"activityguard/internal/detection"
	"activityguard/internal/scheduler"
	"activityguard/internal/seed"
)

func main() {
	cfg := config.Load()

	gdb, err := db.Open(cfg)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	if err := db.Migrate(gdb); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if err := seed.Run(gdb); err != nil {
		log.Fatalf("seed: %v", err)
	}

	engine := detection.New(gdb, cfg)
	srv := api.NewServer(gdb, cfg, engine)
	httpSrv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           srv.Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// 启动时先捡取遗留的待处理事件（上次进程可能在检测前退出）。
	if n, err := engine.ProcessPending(5000); err != nil {
		log.Printf("startup pending recovery error: %v", err)
	} else if n > 0 {
		log.Printf("startup: processed %d pending event(s)", n)
	}

	if cfg.SchedulerEnabled {
		sched := scheduler.New(gdb, engine, cfg)
		go sched.Run(ctx)
	}

	go func() {
		log.Printf("api listening on %s", cfg.HTTPAddr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
		os.Exit(1)
	}
	log.Printf("stopped cleanly")
}
