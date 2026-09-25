// Command logcluster runs the HTTP log-template clustering backend.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"logcluster/internal/cluster"
	"logcluster/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	data := flag.String("data", "", "snapshot file for local persistence (empty = in-memory only)")
	capacity := flag.Int("capacity", cluster.DefaultMaxTemplates, "max live templates (LRU eviction beyond this)")
	maxLine := flag.Int("max-line-bytes", cluster.DefaultMaxLineBytes, "lines longer than this are truncated and marked <TRUNC>")
	maxTokens := flag.Int("max-tokens", cluster.DefaultMaxTokens, "token lists longer than this are truncated and marked <TRUNC>")
	maxWild := flag.Float64("max-wild-ratio", cluster.DefaultMaxWildRatio, "max wildcard fraction per template (over-generalization guard)")
	flag.Parse()

	cfg := cluster.Config{
		MaxTemplates: *capacity,
		MaxLineBytes: *maxLine,
		MaxTokens:    *maxTokens,
		MaxWildRatio: *maxWild,
	}
	cl, err := server.LoadOrNew(cfg, *data)
	if err != nil {
		log.Fatalf("load snapshot: %v", err)
	}
	srv := server.New(cl, *data)

	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		if err := srv.SaveSnapshot(); err != nil {
			log.Printf("save snapshot on shutdown: %v", err)
		} else if *data != "" {
			log.Printf("snapshot saved to %s", *data)
		}
		os.Exit(0)
	}()

	st := cl.Stats()
	log.Printf("listening on %s (templates=%d ingested=%d capacity=%d persistence=%q)",
		*addr, st.Templates, st.Ingested, st.Capacity, *data)
	log.Fatal(http.ListenAndServe(*addr, srv.Handler()))
}
