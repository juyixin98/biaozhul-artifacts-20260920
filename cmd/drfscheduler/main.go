// Command drfscheduler runs the local HTTP API for the two-resource DRF
// scheduler. It supports a deterministic simulation mode (fake clock +
// simulated executor, advanced via POST /v1/clock/advance) and a real-time
// mode (wall clock + simulated sleeps or real OS processes).
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"drfscheduler/pkg/httpapi"
	"drfscheduler/pkg/scheduler"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	cpu := flag.Int64("capacity-cpu", 10000, "cluster CPU capacity in millicpu")
	mem := flag.Int64("capacity-mem", 10240, "cluster memory capacity in MiB")
	mode := flag.String("mode", "sim", "execution mode: sim (fake clock) | real (wall clock)")
	executor := flag.String("executor", "sim", "executor type: sim | process (process requires -mode=real)")
	eventLog := flag.String("events-log", "", "optional path: append structured events as JSON Lines")
	flag.Parse()

	var (
		clock    scheduler.Clock
		fake     *scheduler.FakeClock
		exec     scheduler.Executor
		sink     scheduler.EventSink
		closeLog func() error
	)

	sink = scheduler.NewMemorySink(0)
	if *eventLog != "" {
		fs, closeFn, err := scheduler.NewFileSinkFromPath(*eventLog)
		if err != nil {
			log.Fatalf("open events log: %v", err)
		}
		closeLog = closeFn
		sink = scheduler.NewMultiSink(scheduler.NewMemorySink(0), fs)
	}

	switch *mode {
	case "sim":
		fake = scheduler.NewFakeClock(time.Time{})
		clock = fake
		if *executor != "sim" {
			log.Fatalf("-executor=process requires -mode=real (a real clock)")
		}
		exec = scheduler.NewSimExecutor(clock)
	case "real":
		clock = scheduler.NewRealClock()
		switch *executor {
		case "sim":
			exec = scheduler.NewSimExecutor(clock)
		case "process":
			exec = scheduler.NewProcessExecutor()
		default:
			log.Fatalf("unknown executor %q", *executor)
		}
	default:
		log.Fatalf("unknown mode %q (want sim|real)", *mode)
	}

	sched, err := scheduler.New(scheduler.Config{
		Capacity: scheduler.Resources{CPU: *cpu, Memory: *mem},
		Clock:    clock,
		Executor: exec,
		Sink:     sink,
	})
	if err != nil {
		log.Fatalf("scheduler init: %v", err)
	}
	sched.Start()
	defer func() {
		_ = sched.Close()
		if closeLog != nil {
			_ = closeLog()
		}
	}()

	server := httpapi.NewServer(sched, fake)
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           withLogging(server.Mux),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("DRF scheduler listening on %s (mode=%s executor=%s capacity cpu=%dm mem=%dMiB)",
			*addr, *mode, *executor, *cpu, *mem)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(ctx)
}

// withLogging logs one line per request.
func withLogging(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		h.ServeHTTP(w, r)
		log.Printf("%s %s (%s)", r.Method, r.URL.RequestURI(), time.Since(start))
	})
}
