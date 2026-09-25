// Command bis starts the build-input-provenance backend service.
//
// It binds to 127.0.0.1 by default, stores everything under --root, and
// makes no outbound network connections. Only tools explicitly declared in
// an imported project's build.json are ever executed.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"bis/internal/api"
	"bis/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address (loopback by design)")
	root := flag.String("root", "./.bis", "service root for cache/work/state (must not contain project sources)")
	timeout := flag.Duration("timeout", 30*time.Second, "per-action execution timeout")
	flag.Parse()

	st, err := store.Open(*root)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	svc := api.NewService(st, *timeout)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           withLogging(svc.Handler()),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("build-input-provenance service listening on %s (root=%s)", *addr, *root)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}

func withLogging(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: 200}
		h.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), rw.status, time.Since(start).Round(time.Millisecond))
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
