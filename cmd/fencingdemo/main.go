// Command fencingdemo runs the lock service and the resource service as two
// independent HTTP servers in one process (local two-component simulation).
//
// The two components share no in-memory state: the lock never calls the
// resource and vice versa. They only share the on-disk data directory.
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

	"fencingdemo/clock"
	"fencingdemo/internal/lock"
	"fencingdemo/internal/resource"
)

// healthz answers 200 OK for readiness/liveness probes.
func healthz(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"ok"}`))
			return
		}
		next.ServeHTTP(w, r)
	})
}

func runServer(ctx context.Context, name, addr string, handler http.Handler) error {
	srv := &http.Server{
		Addr:              addr,
		Handler:           healthz(handler),
		ReadHeaderTimeout: 5 * time.Second,
	}

	serveErr := make(chan error, 1)
	go func() {
		log.Printf("[%s] listening on %s", name, addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
		}
		close(serveErr)
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			return err
		}
		log.Printf("[%s] stopped", name)
		return nil
	case err := <-serveErr:
		return err
	}
}

func main() {
	var (
		lockAddr = flag.String("lock-addr", envOr("LOCK_ADDR", ":8080"), "lock service listen address")
		resAddr  = flag.String("resource-addr", envOr("RESOURCE_ADDR", ":8090"), "resource service listen address")
		dataDir  = flag.String("data-dir", envOr("DATA_DIR", "./data"), "directory for snapshot files")
		ttl      = flag.Duration("ttl", envDurationOr("LOCK_TTL", 10*time.Second), "lock lease TTL")
	)
	flag.Parse()

	if err := os.MkdirAll(*dataDir, 0o755); err != nil {
		log.Fatalf("create data dir: %v", err)
	}

	lockStore, err := lock.NewStore(clock.Real{}, *dataDir+"/lock.json", *ttl)
	if err != nil {
		log.Fatalf("init lock store: %v", err)
	}
	resStore, err := resource.NewStore(*dataDir + "/resource.json")
	if err != nil {
		log.Fatalf("init resource store: %v", err)
	}

	rootCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	ctx, cancel := context.WithCancel(rootCtx)
	defer cancel()

	lockSrv := lock.NewServer(lockStore)
	resSrv := resource.NewServer(resStore)

	errCh := make(chan error, 2)
	go func() { errCh <- runServer(ctx, "lock", *lockAddr, lockSrv.Handler()) }()
	go func() { errCh <- runServer(ctx, "resource", *resAddr, resSrv.Handler()) }()

	// Stop both servers as soon as either one exits (shutdown or failure).
	var firstErr error
	for i := 0; i < 2; i++ {
		if err := <-errCh; err != nil && firstErr == nil {
			firstErr = err
		}
		cancel()
	}
	if firstErr != nil {
		log.Fatalf("server error: %v", firstErr)
	}
	log.Print("both services shut down cleanly")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDurationOr(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
		log.Printf("ignoring invalid %s=%q, using %s", key, v, def)
	}
	return def
}
