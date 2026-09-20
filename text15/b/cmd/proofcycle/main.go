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

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"proofcycle/internal/config"
	"proofcycle/internal/database"
	"proofcycle/internal/httpapi"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

func main() {
	cfg := config.Load()

	gormCfg := &gorm.Config{
		Logger: gormlogger.New(log.New(os.Stdout, "[gorm] ", log.LstdFlags), gormlogger.Config{
			SlowThreshold:             time.Second,
			IgnoreRecordNotFoundError: true,
			LogLevel:                  gormlogger.Warn,
		}),
	}
	db, err := gorm.Open(mysql.Open(cfg.MySQLDSN), gormCfg)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	if err = database.WaitForDB(db, 30, cfg.DBRetryDelay); err != nil {
		log.Fatalf("database not reachable: %v", err)
	}
	if err = database.Migrate(db); err != nil {
		log.Fatalf("run migrations: %v", err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		log.Fatalf("sql db handle: %v", err)
	}
	sqlDB.SetMaxOpenConns(25)
	sqlDB.SetMaxIdleConns(10)
	sqlDB.SetConnMaxLifetime(time.Hour)

	store, err := storage.New(cfg.StorageDir)
	if err != nil {
		log.Fatalf("init storage: %v", err)
	}

	svc := service.New(db, store, cfg.MaxUploadSize)

	// Crash recovery: finish or undo every interrupted upload before serving.
	if err = svc.RecoverOrphans(context.Background()); err != nil {
		log.Fatalf("startup recovery: %v", err)
	}

	if cfg.SeedDemo {
		if err = seed.Seed(context.Background(), db, svc); err != nil {
			log.Fatalf("seed demo data: %v", err)
		}
	}

	bootstrapToken := os.Getenv("PROOFCYCLE_BOOTSTRAP_TOKEN")
	engine := httpapi.NewEngine(svc, bootstrapToken)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           engine,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("ProofCycle listening on %s (storage: %s, max upload: %d MiB)",
			cfg.HTTPAddr, store.Root(), cfg.MaxUploadSize/(1024*1024))
		if serveErr := srv.ListenAndServe(); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			log.Fatalf("http server: %v", serveErr)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Println("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err = srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown: %v", err)
	}
	if err = sqlDB.Close(); err != nil {
		log.Printf("close database: %v", err)
	}
}
