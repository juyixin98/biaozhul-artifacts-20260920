// Command gangd starts the gang scheduler HTTP server.
//
// Usage:
//
//	gangd -addr :8080 -ttl 5s -reap 50ms
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/example/gangscheduler/internal/api"
	"github.com/example/gangscheduler/internal/scheduler"
)

func main() {
	addr := flag.String("addr", envOr("GANGD_ADDR", ":8080"), "listen address")
	ttl := flag.Duration("ttl", 5*time.Second, "default reservation window")
	reap := flag.Duration("reap", 50*time.Millisecond, "reaper interval")
	flag.Parse()

	sched := scheduler.New(scheduler.Config{
		DefaultTTL: *ttl,
		ReapEvery:  *reap,
	})
	defer sched.Stop()

	handler := api.NewServer(sched).Handler()
	srv := &http.Server{
		Addr:              *addr,
		Handler:           loggingMiddleware(handler),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("gangd listening on %s (ttl=%s reap=%s)", *addr, *ttl, *reap)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d %s", r.Method, r.URL.RequestURI(), rw.status, time.Since(start))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
