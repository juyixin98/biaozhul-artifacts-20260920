// Command anomalywatch starts the employee activity anomaly detection API and
// background scheduler. Data stays on the local machine; no external services
// beyond the local MySQL container are contacted.
package main

import (
	"context"
	"database/sql"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/database"
	"anomalywatch/internal/detection"
	"anomalywatch/internal/httpapi"
	"anomalywatch/internal/ingest"
	"anomalywatch/internal/scheduler"
	"anomalywatch/internal/seed"

	"github.com/gin-gonic/gin"
)

func main() {
	migrationsDir := flag.String("migrations", "migrations", "directory containing SQL migrations")
	flag.Parse()

	cfg := config.Load()
	log.Printf("anomalywatch starting; db=%s:%s/%s, listen=%s",
		cfg.DBHost, cfg.DBPort, cfg.DBName, cfg.HTTPAddr)

	if err := database.EnsureDatabase(cfg); err != nil {
		log.Fatalf("ensure database: %v", err)
	}
	if err := runMigrations(cfg, *migrationsDir); err != nil {
		log.Fatalf("migrations: %v", err)
	}

	gdb, err := database.Connect(cfg)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	if err := seed.Run(gdb, cfg); err != nil {
		log.Fatalf("seed: %v", err)
	}

	engine := detection.New(gdb, cfg)
	ingestSvc := ingest.New(gdb, cfg)

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	api := r.Group("/")
	api.Use(httpapi.Auth(gdb))
	h := &httpapi.Handlers{DB: gdb, Cfg: cfg, Ingest: ingestSvc, Engine: engine}
	h.Register(api)

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           r,
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	sched := scheduler.New(engine, cfg)
	go sched.Run(ctx)

	go func() {
		log.Printf("listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}

// runMigrations opens a short-lived multiStatements connection for applying SQL.
func runMigrations(cfg config.Config, dir string) error {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return err
	}
	db, err := sql.Open("mysql", cfg.DSN(true))
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetConnMaxLifetime(time.Minute)
	return database.Migrate(db, abs)
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()
		log.Printf("%s %s -> %d (%s)",
			c.Request.Method, c.Request.URL.Path, c.Writer.Status(), time.Since(start))
	}
}

func init() {
	// Gin's release mode still writes to stdout; keep logs on stderr.
	_ = os.Stderr
}
