// Command hlc-server runs the hybrid logical clock HTTP service.
//
// Configuration (flags or environment variables):
//
//	--addr          / $HLC_ADDR               listen address (default ":8080")
//	--node          / $HLC_NODE_ID            node id (default: hostname)
//	--drift         / $HLC_MAX_DRIFT_MS       max accepted future drift, ms (1000)
//	--max-logical   / $HLC_MAX_LOGICAL        logical counter limit (4294967295)
//	--overflow-wait / $HLC_OVERFLOW_WAIT_MS   max ms to wait for physical time to
//	                                          advance past a saturated counter (250)
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"hlcservice/internal/hlc"
	"hlcservice/internal/server"
)

func main() {
	addr := flag.String("addr", envOr("HLC_ADDR", ":8080"), "listen address")
	nodeID := flag.String("node", envOr("HLC_NODE_ID", defaultNodeID()), "node id")
	maxDrift := flag.Int64("drift", envInt64("HLC_MAX_DRIFT_MS", hlc.DefaultMaxDrift),
		"max accepted future drift in milliseconds")
	maxLogical := flag.Uint64("max-logical", envUint64("HLC_MAX_LOGICAL", hlc.DefaultMaxLogical),
		"logical counter limit")
	overflowWaitMS := flag.Int64("overflow-wait", envInt64("HLC_OVERFLOW_WAIT_MS",
		int64(hlc.DefaultMaxOverflowWait/time.Millisecond)),
		"max milliseconds to wait for physical time to advance on counter overflow")
	flag.Parse()

	clock, err := hlc.NewClock(hlc.Config{
		NodeID:              *nodeID,
		MaxDriftMS:          *maxDrift,
		MaxLogical:          *maxLogical,
		OverflowWaitTimeout: time.Duration(*overflowWaitMS) * time.Millisecond,
	})
	if err != nil {
		log.Fatalf("hlc: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.WithLogging(server.New(clock).Handler()),
		ReadHeaderTimeout: 5 * time.Second,
	}

	// SIGINT/SIGTERM trigger a bounded graceful shutdown.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("hlc-server starting: node=%q addr=%s max_drift=%dms max_logical=%d overflow_wait=%dms",
			clock.NodeID(), *addr, clock.MaxDrift(), clock.MaxLogical(), *overflowWaitMS)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Print("shutting down ...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	log.Print("stopped")
}

func envOr(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}

func envUint64(key string, def uint64) uint64 {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if n, err := strconv.ParseUint(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}

func defaultNodeID() string {
	if host, err := os.Hostname(); err == nil && host != "" {
		return host
	}
	return fmt.Sprintf("node-%d", time.Now().UnixNano())
}
