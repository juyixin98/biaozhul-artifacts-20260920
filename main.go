package main

import (
	"flag"
	"log"
	"net/http"
	"time"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	maxConcurrency := flag.Int("max-concurrency", 8, "max number of JSON-RPC calls executed concurrently")
	flag.Parse()

	gw := NewGateway(*maxConcurrency)
	registerBuiltinMethods(gw)

	mux := http.NewServeMux()
	mux.Handle("/rpc", gw)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})

	srv := &http.Server{
		Addr:              *addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	log.Printf("jsonrpc-gateway listening on %s (max-concurrency=%d), endpoint: POST /rpc", *addr, *maxConcurrency)
	log.Fatal(srv.ListenAndServe())
}
