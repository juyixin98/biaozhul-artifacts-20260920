package server

import (
	"context"
	"errors"
	"log"
	"net/http"
	"runtime"
	"sync/atomic"
	"time"

	"canceltree/internal/client"
	"canceltree/internal/clock"
	"canceltree/internal/tree"
	"canceltree/internal/upstream"
)

// Options constructs a Server.
type Options struct {
	Addr           string
	ClientAttempts int
	// DisableKeepAlives makes each client call use its own connection.
	// Useful in tests so connection counts settle to zero without pool
	// teardown; production leaves it false to reuse the bounded pool.
	DisableKeepAlives bool
}

// Server bundles the HTTP surface, the engine and the embedded fake upstream.
type Server struct {
	httpServer *http.Server
	engine     *tree.Engine
	cl         *client.Logic
	fake       *upstream.Fake
	baseURL    string
	resetURL   string
	obs        *observer
}

// New wires the whole in-process system: API + engine + fault client + fake
// upstream sharing one listener.
func New(opts Options) *Server {
	addr := opts.Addr
	if addr == "" {
		addr = "127.0.0.1:8080"
	}
	clk := clock.Real{}
	cl := client.New(clk, client.Config{
		Attempts:          opts.ClientAttempts,
		Backoff:           20 * time.Millisecond,
		DialTimeout:       2 * time.Second,
		DisableKeepAlives: opts.DisableKeepAlives,
	})
	fake := upstream.NewHandler()

	// The API does not know its own real port until ListenAndServe; paths
	// are resolved at request time from the request Host instead. Engine
	// base is left empty and handler.resolveFromRequest handles it, so we
	// pass a placeholder base that buildTasks overrides per request.
	s := &Server{
		cl:       cl,
		fake:     fake,
		baseURL:  "http://" + addr,
		resetURL: "/upstream/reset",
		obs:      newObserver(),
	}
	s.engine = tree.NewEngine(clk, cl, tree.Config{
		BaseURL:        s.baseURL,
		CleanupTimeout: 2 * time.Second,
	})

	mux := s.buildMux(fake)

	s.httpServer = &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s
}

// Handler returns the HTTP surface without binding a listener, so tests can
// drive the whole system through httptest.Server on an ephemeral port.
func (s *Server) Handler() http.Handler { return s.buildMux(s.fake) }

func (s *Server) buildMux(fake *upstream.Fake) *http.ServeMux {
	mux := http.NewServeMux()
	mux.Handle("/upstream/", http.StripPrefix("/upstream", fake.Handler()))
	mux.HandleFunc("/api/v1/process", s.handleProcess)
	mux.HandleFunc("/api/v1/release", s.handleRelease)
	mux.HandleFunc("/api/v1/diagnostics", s.handleDiagnostics)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("/shutdown", s.handleShutdown)
	return mux
}

// ListenAndServe runs until the server stops.
func (s *Server) ListenAndServe() error {
	err := s.httpServer.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// Shutdown gracefully stops the HTTP server and releases pooled connections.
func (s *Server) Shutdown(ctx context.Context) error {
	err := s.httpServer.Shutdown(ctx)
	s.cl.CloseIdleConnections()
	return err
}

// handleRelease proxies to the embedded fake upstream's hold control.
func (s *Server) handleRelease(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	// Re-dispatch internally against the fake mux by calling its handler
	// with a rewritten path keeps a single control surface.
	r2 := r.Clone(r.Context())
	r2.URL.Path = "/admin/release"
	s.fake.Handler().ServeHTTP(w, r2)
}

type diagnostics struct {
	Goroutines  int              `json:"goroutines"`
	Mem         runtime.MemStats `json:"mem"`
	Connections client.ConnStats `json:"connections"`
	Upstream    upstream.Stats   `json:"upstream"`
	Requests    requestCounters  `json:"requests"`
	Timestamp   time.Time        `json:"timestamp"`
}

type requestCounters struct {
	Total     int64 `json:"total"`
	Succeeded int64 `json:"succeeded"`
	Failed    int64 `json:"failed"`
	Canceled  int64 `json:"canceled"`
}

func (s *Server) handleDiagnostics(w http.ResponseWriter, r *http.Request) {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	writeJSON(w, http.StatusOK, diagnostics{
		Goroutines:  runtime.NumGoroutine(),
		Mem:         ms,
		Connections: s.cl.ConnStats(),
		Upstream:    s.fake.Snapshot(),
		Requests:    s.obs.snapshot(),
		Timestamp:   time.Now().UTC(),
	})
}

// observer holds process-level counters for structured results.
type observer struct {
	total     atomic.Int64
	succeeded atomic.Int64
	failed    atomic.Int64
	canceled  atomic.Int64
}

func newObserver() *observer { return &observer{} }

func (o *observer) record(status string) {
	o.total.Add(1)
	switch status {
	case tree.TreeSucceeded:
		o.succeeded.Add(1)
	case tree.TreeFailed:
		o.failed.Add(1)
	case tree.TreeCanceled:
		o.canceled.Add(1)
	}
}

func (o *observer) ClientCanceled(id string) {
	log.Printf("request %s: client disconnected before response completed; cleanups still ran", id)
}

func (o *observer) snapshot() requestCounters {
	return requestCounters{
		Total:     o.total.Load(),
		Succeeded: o.succeeded.Load(),
		Failed:    o.failed.Load(),
		Canceled:  o.canceled.Load(),
	}
}
