// Command server runs the multi-tenant execution-isolation HTTP service
// locally. All external dependencies are in-process fakes; nothing here
// talks to production systems.
package main

import (
	"flag"
	"log"
	"net/http"

	"example.com/tenantiso/internal/backend"
	"example.com/tenantiso/internal/cache"
	"example.com/tenantiso/internal/clock"
	"example.com/tenantiso/internal/jobs"
	"example.com/tenantiso/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	workers := flag.Int("workers", 2, "shared worker-pool size")
	maxQueue := flag.Int("max-queue-per-tenant", 4, "max in-flight jobs per tenant")
	memBudget := flag.Int64("mem-budget-per-tenant", 1<<20, "per-tenant in-flight memory budget in bytes")
	flag.Parse()

	be := backend.New()
	c := cache.New()
	mgr := jobs.NewManager(jobs.Config{
		Workers:            *workers,
		MaxQueuePerTenant:  *maxQueue,
		MemBudgetPerTenant: *memBudget,
	}, clock.Real{}, be, c)
	defer mgr.Close()

	srv := server.New(mgr, c, be)
	log.Printf("listening on %s (workers=%d max-queue=%d mem-budget=%d)", *addr, *workers, *maxQueue, *memBudget)
	log.Fatal(http.ListenAndServe(*addr, srv))
}
