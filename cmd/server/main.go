package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
	"geoterritory/internal/config"
	"geoterritory/internal/jobs"
	"geoterritory/internal/store"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

func main() {
	cfg := config.FromEnv()

	sqlDB, gormDB := mustConnect(cfg.MySQLDSN)
	defer sqlDB.Close()

	migrator := store.NewMigrator(sqlDB)
	if err := migrator.Up(); err != nil {
		log.Fatalf("migrations failed: %v", err)
	}
	log.Printf("migrations up to date")

	if cfg.SeedSamples {
		if err := store.Seed(gormDB); err != nil {
			log.Fatalf("seed failed: %v", err)
		}
	} else if err := store.EnsureAtLeastOneOrg(gormDB); err != nil {
		log.Fatalf("%v", err)
	}

	r := newRouter(gormDB)

	// Worker runs in-process. Multiple app containers may run it safely;
	// claims are row-locked and late applies are guarded by target_seq.
	runner := jobs.NewRunner(gormDB)
	runner.RecoverAdopted(context.Background())
	workerCtx, stopWorker := context.WithCancel(context.Background())
	go runner.Run(workerCtx)

	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: r, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		log.Printf("GeoTerritory listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Printf("shutting down")
	stopWorker()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}

func mustConnect(dsn string) (*sql.DB, *gorm.DB) {
	var gdb *gorm.DB
	var err error
	deadline := time.Now().Add(60 * time.Second)
	for {
		gdb, err = gorm.Open(mysql.Open(dsn), &gorm.Config{
			// Record-not-found is an expected control-flow signal (seed
			// idempotency, 404 lookups); keep Warn level but silence that one.
			Logger: gormlogger.New(log.New(os.Stderr, "\r\n", log.LstdFlags), gormlogger.Config{
				SlowThreshold:             200 * time.Millisecond,
				LogLevel:                  gormlogger.Warn,
				IgnoreRecordNotFoundError: true,
			}),
		})
		if err == nil {
			sqlDB, derr := gdb.DB()
			if derr == nil {
				if perr := sqlDB.Ping(); perr == nil {
					sqlDB.SetMaxOpenConns(20)
					sqlDB.SetMaxIdleConns(5)
					sqlDB.SetConnMaxLifetime(time.Hour)
					return sqlDB, gdb
				}
			}
		}
		if time.Now().After(deadline) {
			log.Fatalf("cannot connect to MySQL: %v", err)
		}
		log.Printf("waiting for MySQL: %v", err)
		time.Sleep(2 * time.Second)
	}
}
