// cacheserver is the build cache daemon: it computes cache keys, runs
// real builds, stores artifacts in a CAS and metadata in SQLite.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"

	"buildcache/internal/cas"
	"buildcache/internal/server"
	"buildcache/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	data := flag.String("data", "data", "data directory (SQLite + CAS)")
	workspace := flag.String("workspace", ".", "workspace root that task_dir values resolve against")
	flag.Parse()

	if err := os.MkdirAll(*data, 0o755); err != nil {
		log.Fatalf("create data dir: %v", err)
	}

	st, err := store.Open(filepath.Join(*data, "meta.db"))
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	c, err := cas.Open(*data)
	if err != nil {
		log.Fatalf("open cas: %v", err)
	}

	srv, err := server.New(st, c, *workspace, filepath.Join(*data, "tmp"))
	if err != nil {
		log.Fatalf("server: %v", err)
	}

	log.Printf("cacheserver listening on http://%s (data=%s workspace=%s)", *addr, *data, *workspace)
	log.Fatal(http.ListenAndServe(*addr, srv.Handler()))
}
