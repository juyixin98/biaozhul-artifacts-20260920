// Command server runs the deadline-admission scheduler with a local HTTP API.
//
// Two executor back ends are available:
//
//	-executor=script   (default) payloads are "sleep:<ms>[,fail]" scripts
//	                   measured by the wall clock; safe, dependency-free demos.
//	-executor=command  payloads are run via "sh -c" as local processes; bind
//	                   to trusted networks only.
//
// Overrun policy:
//
//	-overrun=kill_at_budget   (default) kill jobs past their declared bound,
//	                          count them as runtime timeouts;
//	-overrun=observe          let them continue to their deadline, recording
//	                          late completions as deadline misses.
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

	"deadlineadm"
	"deadlineadm/clock"
	"deadlineadm/event"
	"deadlineadm/executor"
	"deadlineadm/httpserver"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address (local HTTP API)")
	capacity := flag.Int("capacity", 1, "machine resource units")
	overrun := flag.String("overrun", "kill_at_budget", "runtime overrun policy: kill_at_budget|observe")
	execName := flag.String("executor", "script", "executor backend: script|command")
	eventLog := flag.String("event-log", "", "optional path for structured events as JSON Lines")
	flag.Parse()

	policy := deadlineadm.OverrunPolicy(*overrun)
	if policy != deadlineadm.PolicyKillAtBudget && policy != deadlineadm.PolicyObserve {
		log.Fatalf("invalid -overrun %q", *overrun)
	}

	clk := clock.NewRealClock()

	var exec executor.Executor
	switch *execName {
	case "script":
		exec = executor.NewScriptExecutor(clk)
	case "command":
		exec = executor.NewCommandExecutor()
	default:
		log.Fatalf("invalid -executor %q", *execName)
	}

	var sink event.Sink
	if *eventLog != "" {
		f, err := os.OpenFile(*eventLog, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			log.Fatalf("open event log: %v", err)
		}
		defer f.Close()
		sink = event.NewJSONLSink(f)
		log.Printf("structured events also written to %s", *eventLog)
	}

	sched := deadlineadm.New(deadlineadm.Config{
		Capacity: *capacity,
		Overrun:  policy,
	}, clk, exec, sink)
	sched.Start()
	defer sched.Stop()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpserver.New(sched).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("deadline-admission server listening on http://%s (capacity=%d, overrun=%s, executor=%s)",
			*addr, *capacity, *overrun, *execName)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
