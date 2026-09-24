// Command server runs the multi-arch image selection API.
//
//	go run ./cmd/server -addr :8080 -db data/ociarch.db
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"

	"ociarch/internal/api"
	"ociarch/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dbPath := flag.String("db", "data/ociarch.db", "SQLite database path")
	flag.Parse()

	if dir := filepath.Dir(*dbPath); dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			log.Fatalf("create db dir: %v", err)
		}
	}
	st, err := store.Open(*dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	log.Printf("listening on %s (db=%s)", *addr, *dbPath)
	log.Fatal(http.ListenAndServe(*addr, api.NewRouter(st)))
}
