// Command synapticgod runs the SynapticGo local model experiment backend.
//
// On boot it connects to PostgreSQL, runs embedded migrations, recovers any
// interrupted merges/garbage collections from disk, starts the background
// GC loop and serves the JSON HTTP API.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/synapticgo/synapticgo/internal/api"
	"github.com/synapticgo/synapticgo/internal/config"
	"github.com/synapticgo/synapticgo/internal/database"
	"github.com/synapticgo/synapticgo/internal/dataset"
	model "github.com/synapticgo/synapticgo/internal/model"
	"github.com/synapticgo/synapticgo/internal/storage"
	"github.com/synapticgo/synapticgo/internal/user"
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "probe /healthz on SYN_HTTP_ADDR and exit")
	flag.Parse()
	if *healthcheck {
		runHealthcheck()
		return
	}
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	db, err := database.Connect(cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer db.Close()

	if err := database.Migrate(db); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	store, err := storage.New(cfg.DataDir)
	if err != nil {
		log.Fatalf("storage: %v", err)
	}

	datasetSvc := &dataset.Service{DB: db, Store: store}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := datasetSvc.RecoverAtStartup(ctx); err != nil {
		log.Fatalf("startup recovery: %v", err)
	}
	go datasetSvc.RunGC(ctx)

	modelSvc := &model.Service{DB: db, Datasets: datasetSvc}
	userSvc := &user.Service{DB: db}

	e := api.NewServer(api.Deps{
		Cfg: cfg, Users: userSvc, Datasets: datasetSvc, Models: modelSvc,
	})
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           e,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("synapticgod listening on %s (data dir %s)", cfg.HTTPAddr, cfg.DataDir)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()
	<-ctx.Done()
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
		os.Exit(1)
	}
}

// runHealthcheck performs a minimal HTTP probe used by the container
// HEALTHCHECK. It derives the port from SYN_HTTP_ADDR (default :8080).
func runHealthcheck() {
	addr := config.LoadAddrForHealth()
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://127.0.0.1" + addr + "/healthz")
	if err != nil {
		os.Exit(1)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		os.Exit(1)
	}
}
