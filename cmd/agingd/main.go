// Command agingd runs the aging priority queue with a local HTTP API.
//
// Configuration (flags, override-able by AGINGD_* environment variables):
//
//	-addr           listen address            (AGINGD_ADDR,        ":8080")
//	-min-priority   lowest priority value     (AGINGD_MIN_PRIORITY, "0")
//	-max-priority   highest priority value    (AGINGD_MAX_PRIORITY, "9")
//	-age-interval   wait per effective level  (AGINGD_AGE_INTERVAL, "1s")
//	-concurrency    parallel attempts         (AGINGD_CONCURRENCY,  "1")
//	-max-attempts   default attempts per job  (AGINGD_MAX_ATTEMPTS, "3")
//	-events-file    append events as JSONL    (AGINGD_EVENTS_FILE,  "")
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"agingqueue/executor"
	"agingqueue/httpapi"
	"agingqueue/queue"
)

func env(key, def string) string {
	if v := os.Getenv("AGINGD_" + key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv("AGINGD_" + key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func main() {
	addr := flag.String("addr", env("ADDR", ":8080"), "listen address")
	minP := flag.Int("min-priority", envInt("MIN_PRIORITY", 0), "lowest priority")
	maxP := flag.Int("max-priority", envInt("MAX_PRIORITY", 9), "highest priority")
	age := flag.Duration("age-interval", parseDur(env("AGE_INTERVAL", "1s"), time.Second), "wait per effective-priority level")
	conc := flag.Int("concurrency", envInt("CONCURRENCY", 1), "parallel attempts")
	maxAttempts := flag.Int("max-attempts", envInt("MAX_ATTEMPTS", 3), "default max attempts")
	eventsFile := flag.String("events-file", env("EVENTS_FILE", ""), "append structured events as JSONL to this file")
	flag.Parse()

	var sink queue.Sink
	memSink := queue.NewMemorySink(10000)
	sink = memSink
	var fileSink *queue.JSONLSink
	if *eventsFile != "" {
		fs, err := queue.NewJSONLFileSink(*eventsFile)
		if err != nil {
			log.Fatalf("open events file: %v", err)
		}
		fileSink = fs
		sink = queue.MultiSink{memSink, fs}
	}

	sched, err := queue.New(queue.Config{
		Executor:           executor.NewRegistry(),
		Sink:               sink,
		MinPriority:        *minP,
		MaxPriority:        *maxP,
		AgeInterval:        *age,
		Concurrency:        *conc,
		DefaultMaxAttempts: *maxAttempts,
	})
	if err != nil {
		log.Fatalf("scheduler: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.NewServer(sched),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("agingd listening on %s (priorities %d..%d, age interval %s, concurrency %d)",
			*addr, *minP, *maxP, *age, *conc)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	if err := sched.Close(); err != nil {
		log.Printf("scheduler close: %v", err)
	}
	if fileSink != nil {
		if err := fileSink.Close(); err != nil {
			log.Printf("events file close: %v", err)
		}
	}
	fmt.Fprintln(os.Stderr, "bye")
}

func parseDur(s string, def time.Duration) time.Duration {
	d, err := time.ParseDuration(s)
	if err != nil {
		return def
	}
	return d
}
