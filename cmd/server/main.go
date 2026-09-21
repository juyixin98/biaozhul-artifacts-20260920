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

	"proofcycle/internal/api"
	"proofcycle/internal/config"
	"proofcycle/internal/db"
	"proofcycle/internal/models"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("configuration error: %v", err)
	}

	gdb, err := openWithRetry(cfg.MySQLDSN)
	if err != nil {
		log.Fatalf("database unavailable: %v", err)
	}
	if err := db.Migrate(gdb); err != nil {
		log.Fatalf("migration failed: %v", err)
	}
	if err := seed.Users(gdb); err != nil {
		log.Fatalf("seeding users failed: %v", err)
	}

	store, err := storage.New(cfg.StorageRoot, cfg.MaxUploadSize)
	if err != nil {
		log.Fatalf("storage init failed: %v", err)
	}
	sweepOrphans(gdb, store)

	gin.SetMode(gin.ReleaseMode)
	h := api.New(gdb, service.New(gdb, store), cfg.MaxUploadSize)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           h.Router(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		log.Printf("ProofCycle listening on %s (max upload %d bytes)", cfg.HTTPAddr, cfg.MaxUploadSize)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server error: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

// openWithRetry waits for MySQL (e.g. docker compose startup) for up to 60s.
func openWithRetry(dsn string) (*gorm.DB, error) {
	var lastErr error
	for attempt := 0; attempt < 30; attempt++ {
		gdb, err := db.Open(dsn)
		if err == nil {
			if perr := db.Ping(gdb); perr == nil {
				return gdb, nil
			} else {
				lastErr = perr
			}
		} else {
			lastErr = err
		}
		time.Sleep(2 * time.Second)
	}
	return nil, lastErr
}

// sweepOrphans cleans interrupted uploads (*.part) and, on a best-effort
// basis, blobs whose database record never committed. This closes the crash
// window between file-on-disk and DB-insert; the normal path removes the blob
// inside the same request.
func sweepOrphans(gdb *gorm.DB, store *storage.Store) {
	var known []string
	if err := gdb.Model(&models.FileVersion{}).Select("storage_path").Scan(&known).Error; err != nil {
		log.Printf("orphan sweep skipped: %v", err)
		return
	}
	knownSet := make(map[string]struct{}, len(known))
	for _, p := range known {
		knownSet[p] = struct{}{}
	}
	partials, blobs, err := store.SweepOrphans(knownSet)
	if err != nil {
		log.Printf("orphan sweep error: %v", err)
		return
	}
	if partials > 0 || blobs > 0 {
		log.Printf("orphan sweep removed %d interrupted upload(s) and %d uncommitted blob(s)", partials, blobs)
	}
}
