// Package server wires the idempotent Service to HTTP and exposes the
// local observability endpoints used by the tests.
package server

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"idemresp/internal/idem"
	"idemresp/internal/txn"
)

// Header names accepted on POST /v1/orders.
const (
	HeaderIdempotencyKey = "Idempotency-Key"
	HeaderForwardKey     = "X-Forward-Key"
	HeaderFault          = "X-Fault"
	HeaderPreDelay       = "X-Pre-Commit-Delay"
	HeaderPostDelay      = "X-Post-Commit-Delay"
	HeaderCrash          = "X-Crash"
)

// maxBodyBytes caps request bodies.
const maxBodyBytes = 1 << 20

// Deps are the server dependencies.
type Deps struct {
	Service    *idem.Service
	DB         *txn.DB
	Logger     *log.Logger
	AllowCrash bool
}

// New builds the application HTTP handler (the gateway runs separately).
func New(d Deps) http.Handler {
	if d.Logger == nil {
		d.Logger = log.New(io.Discard, "", 0)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/orders", d.handleCreateOrder)
	mux.HandleFunc("GET /v1/orders", d.handleGetOrder)
	mux.HandleFunc("GET /v1/ledger", d.handleLedger)
	mux.HandleFunc("GET /v1/keys/", d.handleKey)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	return logging(d.Logger, mux)
}

func (d *Deps) handleCreateOrder(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimSpace(r.Header.Get(HeaderIdempotencyKey))
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key",
			"send an Idempotency-Key header on POST /v1/orders")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "unreadable_body", err.Error())
		return
	}
	if len(body) > maxBodyBytes {
		writeError(w, http.StatusRequestEntityTooLarge, "body_too_large", "request body exceeds 1 MiB")
		return
	}

	in := idem.Input{
		Key:    key,
		Method: r.Method,
		Path:   "/v1/orders",
		Body:   body,
		Fault:  r.Header.Get(HeaderFault),
	}
	// End-to-end key forwarding is on by default. Explicitly send
	// "X-Forward-Key: false" to demonstrate keyless (double-charge)
	// behavior against the fake gateway.
	if v := r.Header.Get(HeaderForwardKey); v == "" {
		in.ForwardKey = true
	} else {
		in.ForwardKey = truthy(v)
	}
	if v := r.Header.Get(HeaderPreDelay); v != "" {
		in.PreCommitDelay = parseDur(v)
	}
	if v := r.Header.Get(HeaderPostDelay); v != "" {
		in.PostCommitDelay = parseDur(v)
	}
	if d.AllowCrash {
		in.CrashPoint = r.Header.Get(HeaderCrash)
	}

	// Detached context: once the claim exists, business work and the
	// atomic commit complete even if the TCP client goes away. A retry of
	// the same key then replays the stored response instead of re-running
	// the side effect. Bounded by an overall deadline so a wedged upstream
	// cannot pin a goroutine forever.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	out := d.Service.Execute(ctx, in)

	if out.InProgress {
		if out.RetryAfter > 0 {
			w.Header().Set(idem.HeaderRetryAfter, strconv.Itoa(int(out.RetryAfter.Seconds())+1))
		}
	}
	if out.Replayed {
		w.Header().Set(idem.HeaderReplay, "true")
	}
	w.Header().Set(idem.HeaderKey, key)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(out.Status)
	_, _ = w.Write(append(out.Body, '\n'))
}

func (d *Deps) handleGetOrder(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	if id == "" {
		writeError(w, http.StatusBadRequest, "missing_id", "provide ?id=<order id>")
		return
	}
	snap := d.DB.Snapshot()
	o, ok := snap.Orders[id]
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", "no such order")
		return
	}
	writeJSON(w, http.StatusOK, o)
}

func (d *Deps) handleLedger(w http.ResponseWriter, r *http.Request) {
	snap := d.DB.Snapshot()
	keyFilter := r.URL.Query().Get("key")
	entries := make([]txn.LedgerEntry, 0)
	for _, e := range snap.Ledger {
		if keyFilter != "" && e.IdemKey != keyFilter {
			continue
		}
		entries = append(entries, e)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"count":   len(entries),
		"entries": entries,
	})
}

func (d *Deps) handleKey(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.URL.Path, "/v1/keys/")
	snap := d.DB.Snapshot()
	row, ok := snap.Keys[key]
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", "no such idempotency key")
		return
	}
	writeJSON(w, http.StatusOK, row)
}

// CrashFn is the real process crash used when X-Crash is enabled.
func CrashFn(stage string) {
	log.Printf("CRASH injected at %s; exiting 77", stage)
	os.Exit(77)
}

func truthy(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func parseDur(v string) time.Duration {
	d, err := time.ParseDuration(v)
	if err != nil {
		return 0
	}
	return d
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": code, "message": msg})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		// Only reached for unreachable internal marshal failures.
		_ = err
	}
}

// statusError lets the logging middleware record status codes.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// Flush implements http.Flusher so middleware stays transparent.
func (r *statusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func logging(lg *log.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		// Don't noise health checks.
		if r.URL.Path != "/healthz" {
			lg.Printf("%s %s -> %d", r.Method, r.URL.RequestURI(), rec.status)
		}
	})
}
