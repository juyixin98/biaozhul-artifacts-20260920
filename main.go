// Command gangsrv runs the in-memory gang scheduler HTTP server.
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

	"gangscheduler/api"
	"gangscheduler/gang"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	ttl := flag.Duration("ttl", 15*time.Second, "default reservation TTL")
	sweep := flag.Duration("sweep", 200*time.Millisecond, "reservation-expiry sweep interval")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	sched := gang.NewScheduler(gang.WithDefaultTTL(*ttl), gang.WithSweepInterval(*sweep))
	sched.Start(ctx)
	defer sched.Stop()

	srv := &api.Server{Sched: sched}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           logRequests(srv.NewRouter()),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("gang scheduler listening on %s (default ttl=%s, sweep=%s)", *addr, *ttl, *sweep)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
		os.Exit(1)
	}
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		h.ServeHTTP(sw, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), sw.status, time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}
