// Command vcregd runs the vector-clock multi-version register simulation as
// an HTTP server. Replicas are created up front with -replicas (or later via
// PUT /replicas/{id}); they share a process but no state.
package main

import (
	"flag"
	"log"
	"net/http"
	"strings"

	"vcreg/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	replicas := flag.String("replicas", "r1,r2,r3", "comma-separated replica ids to create up front")
	flag.Parse()

	cluster := server.NewCluster()
	for _, id := range strings.Split(*replicas, ",") {
		id = strings.TrimSpace(id)
		if id != "" {
			cluster.Ensure(id)
		}
	}

	log.Printf("vcregd listening on %s (replicas: %s)", *addr, strings.Join(cluster.IDs(), ", "))
	if err := http.ListenAndServe(*addr, server.Handler(cluster)); err != nil {
		log.Fatal(err)
	}
}
