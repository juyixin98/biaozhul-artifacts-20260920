// Command server runs the graceful-shutdown demo HTTP service. All external
// dependencies are in-process fakes; nothing talks to production systems.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"graceful-shutdown/internal/clock"
	"graceful-shutdown/internal/fakesvc"
	"graceful-shutdown/internal/ledger"
	"graceful-shutdown/internal/resources"
	"graceful-shutdown/internal/server"
	"graceful-shutdown/internal/shutdown"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	drainTimeout := flag.Duration("drain-timeout", 2*time.Second, "max time to drain in-flight work before cancelling")
	postDoneGrace := flag.Duration("post-done-grace", 3*time.Second, "how long /state and /healthz stay served after shutdown completes")
	flag.Parse()

	clk := clock.Real{}
	led := ledger.New()
	reg := resources.NewRegistry()

	// Registration order defines close order: LIFO, so fake-queue closes
	// before fake-db.
	db := fakesvc.NewFakeDB(clk)
	queue := fakesvc.NewFakeQueue(clk)
	reg.Register(db)
	reg.Register(queue)

	coord := shutdown.New(clk, *drainTimeout, led, reg)
	srv := server.New(coord, clk, db, queue)

	httpSrv := &http.Server{Addr: *addr, Handler: srv.Handler()}

	// OS signals trigger the same idempotent shutdown as POST /shutdown.
	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		for s := range sigCh {
			log.Printf("received signal %v: triggering graceful shutdown (idempotent)", s)
			coord.Shutdown()
		}
	}()

	go func() {
		log.Printf("listening on %s (drain-timeout=%s)", *addr, *drainTimeout)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server error: %v", err)
		}
	}()

	<-coord.Done()
	state := coord.State()
	log.Printf("shutdown complete: phase=%s drain_timed_out=%v signals=%d close_order=%v",
		state.Phase, state.DrainTimedOut, state.ShutdownSignals, state.CloseOrder)

	// Keep /state and /healthz reachable briefly so the fault-injection
	// client can fetch the final structured state, then exit cleanly.
	clk.Sleep(*postDoneGrace)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("http server shutdown: %v", err)
	}
	log.Printf("process exiting")
}
