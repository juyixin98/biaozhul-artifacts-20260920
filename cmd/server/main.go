// Command server runs the cgroup v2 offline sampling analysis service.
package main

import (
	"flag"
	"log"
	"net/http"

	"cgroup-analyzer/internal/api"
	"cgroup-analyzer/internal/fixture"
	"cgroup-analyzer/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	db := flag.String("db", ":memory:", "SQLite DSN (e.g. file:data.db or :memory:)")
	fixtures := flag.String("fixtures", "fixtures", "fixture root directory (must contain containers/)")
	flag.Parse()

	st, err := store.Open(*db)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer st.Close()

	insts, err := fixture.Load(*fixtures)
	if err != nil {
		log.Fatalf("load fixtures from %s: %v", *fixtures, err)
	}
	if err := st.ReplaceInstances(insts); err != nil {
		log.Fatalf("ingest fixtures: %v", err)
	}
	n := 0
	for _, inst := range insts {
		n += len(inst.Samples)
	}
	log.Printf("ingested %d instance(s), %d sample(s) from %s", len(insts), n, *fixtures)
	log.Printf("listening on %s", *addr)
	log.Fatal(http.ListenAndServe(*addr, api.NewServer(st, *fixtures).Router()))
}
