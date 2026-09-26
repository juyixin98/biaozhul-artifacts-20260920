// Command contractcheckd runs the contract compatibility checker together
// with an in-process fake contract registry. Nothing talks to production
// systems; both servers bind to loopback by default.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"contractcheck/internal/api"
	"contractcheck/internal/clock"
	"contractcheck/internal/fault"
	"contractcheck/internal/registry"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address of the checker service")
	registryAddr := flag.String("registry-addr", "127.0.0.1:9001", "listen address of the in-process fake registry")
	faultLatency := flag.Duration("fault-latency", 0, "latency injected into every registry call")
	faultErrorRate := flag.Float64("fault-error-rate", 0, "probability [0,1] of an injected registry error")
	flag.Parse()

	store := registry.NewStore()
	seed(store)
	registryServer := &http.Server{Addr: *registryAddr, Handler: store.Handler()}
	go func() {
		log.Printf("fake registry listening on http://%s", *registryAddr)
		if err := registryServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("registry: %v", err)
		}
	}()

	transport := fault.NewTransport(nil, clock.Real{}, fault.Config{
		Latency:   *faultLatency,
		ErrorRate: *faultErrorRate,
	})
	transport.AllowHeaderOverride = true
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}

	srv := api.NewServer("http://"+*registryAddr, client, clock.Real{})
	log.Printf("compatibility checker listening on http://%s", *addr)
	log.Fatal(http.ListenAndServe(*addr, srv))
}

// seed loads sample contracts: orders@v2 intentionally breaks orders@v1 in
// both directions so the checker has a real counterexample to report.
func seed(store *registry.Store) {
	store.Put(registry.Contract{
		Service: "orders",
		Version: "v1",
		Request: []byte(`{
			"type": "object",
			"required": ["orderId", "amount"],
			"properties": {
				"orderId": {"type": "string"},
				"amount": {"type": "number", "minimum": 0},
				"currency": {"type": "string", "enum": ["USD", "EUR", "CNY"]}
			}
		}`),
		Response: []byte(`{
			"type": "object",
			"required": ["status"],
			"properties": {
				"status": {"type": "string", "enum": ["ok", "failed"]},
				"etaDays": {"type": "integer", "minimum": 0, "maximum": 30}
			}
		}`),
	})
	store.Put(registry.Contract{
		Service: "orders",
		Version: "v2",
		Request: []byte(`{
			"type": "object",
			"required": ["orderId", "amount", "currency"],
			"properties": {
				"orderId": {"type": "string"},
				"amount": {"type": "number", "minimum": 1},
				"currency": {"type": "string", "enum": ["USD", "EUR"]}
			}
		}`),
		Response: []byte(`{
			"type": "object",
			"required": ["status"],
			"properties": {
				"status": {"type": "string", "enum": ["ok", "failed", "pending"]},
				"etaDays": {"type": "integer", "minimum": 0, "maximum": 60}
			}
		}`),
	})
}
