// Command server runs the local three-layer demo service:
//
//	POST /call -> layer1 -> layer2 -> fake external service (in-process)
//
// Retry budget, deadline and idempotency are propagated via X-Retry-Budget-
// Remaining, X-Deadline-Unix-Milli and X-Idempotent headers. The fake
// external dependency is fault-injectable via X-Fault-* headers (see
// internal/fakesvc). Nothing talks to any production system.
package main

import (
	"flag"
	"log"
	"math/rand"
	"net/http"
	"time"

	"github.com/example/retrybudget/internal/chain"
	"github.com/example/retrybudget/internal/clock"
	"github.com/example/retrybudget/internal/fakesvc"
	"github.com/example/retrybudget/internal/retry"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	budget := flag.Int64("budget", 8, "root retry budget used when the request carries none")
	seed := flag.Int64("seed", 1, "backoff jitter random seed")
	flag.Parse()

	caller := retry.Caller{
		Clock: clock.Real{},
		Backoff: retry.Backoff{
			Base:       50 * time.Millisecond,
			Max:        800 * time.Millisecond,
			Multiplier: 2,
			Rand:       rand.New(rand.NewSource(*seed)),
		},
	}
	fake := fakesvc.New()
	store := retry.NewBudgetStore()
	layer2 := &chain.Layer{
		Name:          "layer2",
		DefaultBudget: *budget,
		MaxDuration:   30 * time.Second,
		Caller:        caller,
		Next:          chain.HandlerTransport{Name: "layer2->fake", Handler: fake},
		Store:         store,
	}
	layer1 := &chain.Layer{
		Name:          "layer1",
		DefaultBudget: *budget,
		MaxDuration:   30 * time.Second,
		Caller:        caller,
		Next:          chain.HandlerTransport{Name: "layer1->layer2", Handler: layer2},
		Store:         store,
	}

	mux := http.NewServeMux()
	mux.Handle("/call", layer1)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte("ok\n"))
	})

	log.Printf("listening on %s (root budget %d); POST /call to exercise the chain", *addr, *budget)
	log.Fatal(http.ListenAndServe(*addr, mux))
}
