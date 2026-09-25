// Command server runs the histogram-merge observability backend.
package main

import (
	"flag"
	"log"
	"net/http"

	"histmerge/internal/server"
	"histmerge/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	data := flag.String("data", "data/histograms.json", "local persistence file (empty = in-memory only)")
	flag.Parse()

	st, err := store.Open(*data)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	srv := server.New(st)
	log.Printf("histogram-merge backend listening on http://%s (data file: %s)", *addr, *data)
	log.Fatal(http.ListenAndServe(*addr, srv))
}
