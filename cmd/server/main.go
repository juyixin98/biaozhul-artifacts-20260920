// Command proofcycle-server 启动 ProofCycle 包装打样审查后端。
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

	"proofcycle/internal/config"
	"proofcycle/internal/database"
	"proofcycle/internal/httpapi"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

func main() {
	cfg := config.FromEnv()

	db, err := database.Open(cfg.MySQLDSN, 90*time.Second)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	if cfg.AutoMigrate {
		if err := database.Migrate(db); err != nil {
			log.Fatalf("migrate: %v", err)
		}
		log.Printf("database migrated")
	}

	store, err := storage.NewLocalStore(cfg.StorageRoot)
	if err != nil {
		log.Fatalf("init storage: %v", err)
	}
	// 启动恢复：清理上次进程上传中断残留在 tmp 目录的文件。
	if err := store.SweepTemp(); err != nil {
		log.Printf("warn: sweep temp uploads: %v", err)
	}

	if cfg.Seed {
		if err := seed.Ensure(db); err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Printf("demo data ensured")
	}

	svc := service.New(db, store, cfg.MaxUploadSize)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           httpapi.New(svc, cfg.MaxUploadSize).Handler(),
		ReadHeaderTimeout: 15 * time.Second,
	}

	go func() {
		log.Printf("ProofCycle listening on %s (storage=%s, maxUpload=%d bytes)",
			cfg.HTTPAddr, cfg.StorageRoot, cfg.MaxUploadSize)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
