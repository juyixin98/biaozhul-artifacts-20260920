// Command fairdrf starts the local HTTP service for the two-resource
// weighted DRF scheduler.
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"

	"fairdrf/scheduler"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	cpuMilli := flag.Int64("cpu-milli", 10000, "cluster CPU capacity in milliCPU (1000 = 1 core)")
	memMiB := flag.Int64("mem-mib", 10240, "cluster memory capacity in MiB")
	flag.Parse()

	sched, err := scheduler.New(scheduler.Resources{CPU: *cpuMilli, Mem: *memMiB})
	if err != nil {
		log.Fatalf("scheduler init: %v", err)
	}
	srv := scheduler.NewServer(sched)

	log.Printf("fairdrf listening on %s (capacity cpu=%dm mem=%dMiB)", *addr, *cpuMilli, *memMiB)
	if err := http.ListenAndServe(*addr, srv.Handler()); err != nil {
		log.Fatalf("server: %v", fmt.Sprint(err))
	}
}
