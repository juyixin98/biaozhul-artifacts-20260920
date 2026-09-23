// Command raftlab serves the Raft simulator HTTP API.
//
// Usage:
//
//	go run ./cmd/raftlab [-addr :8080]
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"

	"raftlab/httpx"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()

	mux := http.NewServeMux()
	srv := httpx.New()
	srv.Routes(mux)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprint(w, helpText)
	})

	log.Printf("Raft lab API listening on %s (see README.md for curl examples)", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}

const helpText = `Raft log-replication lab - deterministic simulator API

Endpoints:
  POST /sessions                       create cluster {nodes,variant,seed,trace}
  GET  /sessions/{id}                  snapshot nodes + trace
  POST /sessions/{id}/advance          {ms}
  POST /sessions/{id}/partition        {"isolate":[2]} or {"groups":[[1],[2,3]]}
  POST /sessions/{id}/heal             clear partitions
  POST /sessions/{id}/pause            {node} hold inbound messages
  POST /sessions/{id}/resume           {node} release them
  POST /sessions/{id}/pause-from       {node} hold outbound messages
  POST /sessions/{id}/resume-from      {node} release them
  POST /sessions/{id}/restart          {node} crash + restore from disk
  POST /sessions/{id}/propose          {node?,command} node 0 => current leader
  POST /sessions/{id}/inject-stale-append {from,to,term,index,command}
  POST /scenarios/run                  run a scenario JSON document
  GET  /scenarios/counterexamples      canonical buggy-variant counterexamples
  POST /enumerate                      {variant,depth,fuzz,maxCounterexamples}

Variants: standard (safe), notermcheck (delayed-message bug), naive (+one-quorum commit bug)
`
