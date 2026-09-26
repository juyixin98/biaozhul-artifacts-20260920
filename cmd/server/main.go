// Command server runs the local multi-tenant compute service. All external
// dependencies are in-process fakes; nothing contacts a production system.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"tenantiso/internal/cache"
	"tenantiso/internal/clock"
	"tenantiso/internal/engine"
	"tenantiso/internal/fakestore"
	"tenantiso/internal/server"
)

func main() {
	var (
		addr         = flag.String("addr", "127.0.0.1:8080", "listen address")
		workers      = flag.Int("workers", 2, "worker pool size")
		queueDepth   = flag.Int("queue-depth", 4, "per-tenant queue depth")
		inFlight     = flag.Int("in-flight-jobs", 2, "per-tenant max running jobs")
		memBudget    = flag.Int64("mem-budget", 16<<20, "per-tenant in-flight memory budget (bytes)")
		maxJobMem    = flag.Int64("max-job-mem", 8<<20, "per-job memory ceiling (bytes)")
		maxWork      = flag.Int("max-work", 1_000_000_000, "per-job work ceiling")
		storeLatency = flag.Duration("store-latency", 0, "simulated fake-store latency")
	)
	flag.Parse()

	clk := clock.Real{}
	store := fakestore.New(clk)
	store.SetLatency(*storeLatency)
	c := cache.New(store, "compute")
	limits := engine.Limits{
		MaxQueueDepth:       *queueDepth,
		MaxInFlightJobs:     *inFlight,
		MaxInFlightMemBytes: *memBudget,
		MaxJobMemBytes:      *maxJobMem,
		MaxWork:             *maxWork,
	}
	eng := engine.NewScheduler(clk, c, limits, *workers)
	eng.Start()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(eng, c, clk, limits),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		log.Println("shutting down")
		eng.Stop()
		os.Exit(0)
	}()

	log.Printf("listening on %s (workers=%d queue-depth=%d in-flight=%d mem-budget=%d)",
		*addr, *workers, *queueDepth, *inFlight, *memBudget)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
