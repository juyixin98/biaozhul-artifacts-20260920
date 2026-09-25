// Command logcluster runs the HTTP ingestion/query server with local,
// snapshot-based persistence.
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

	"logcluster/internal/engine"
	"logcluster/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	snapshotPath := flag.String("snapshot", "data/snapshot.json", "snapshot file path")
	maxClusters := flag.Int("max-clusters", 500, "maximum active template clusters (capacity bound)")
	ringCap := flag.Int("ring-capacity", 5000, "maximum stored events")
	maxLine := flag.Int("max-line-bytes", 16*1024, "maximum bytes retained per line")
	autosave := flag.Duration("autosave", 30*time.Second, "periodic snapshot interval (0 disables)")
	flag.Parse()

	logger := log.New(os.Stderr, "logcluster: ", log.LstdFlags)

	cfg := engine.Config{
		MaxClusters:   *maxClusters,
		RingCapacity:  *ringCap,
		MaxLineBytes:  *maxLine,
		MaxEvictedLog: 100,
	}
	eng := engine.New(cfg, nil)

	if loaded, err := eng.LoadIfExists(*snapshotPath); err != nil {
		logger.Printf("WARNING: cannot load snapshot %s: %v (starting fresh)", *snapshotPath, err)
	} else if loaded {
		st := eng.Stats()
		logger.Printf("restored snapshot: %d templates, %d events from %s",
			st.ActiveClusters, st.StoredEvents, *snapshotPath)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(eng, logger).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Periodic snapshot.
	var autoDone chan struct{}
	if *autosave > 0 {
		autoDone = make(chan struct{})
		go func() {
			t := time.NewTicker(*autosave)
			defer t.Stop()
			for {
				select {
				case <-t.C:
					if err := eng.Save(*snapshotPath); err != nil {
						logger.Printf("autosave failed: %v", err)
					}
				case <-ctx.Done():
					close(autoDone)
					return
				}
			}
		}()
	}

	serverErr := make(chan error, 1)
	go func() {
		logger.Printf("listening on %s (max-clusters=%d, ring=%d)", *addr, *maxClusters, *ringCap)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
		close(serverErr)
	}()

	select {
	case <-ctx.Done():
		logger.Printf("shutdown signal received, saving snapshot...")
	case err := <-serverErr:
		if err != nil {
			logger.Printf("server error: %v", err)
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("HTTP shutdown: %v", err)
	}
	if autoDone != nil {
		<-autoDone
	}
	if err := eng.Save(*snapshotPath); err != nil {
		logger.Printf("final snapshot save failed: %v", err)
	} else {
		logger.Printf("snapshot written to %s", *snapshotPath)
	}
}
