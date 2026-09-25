// Command logmerge runs the multi-line log merging backend.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"syscall"
	"time"

	"logmerge/internal/merger"
	"logmerge/internal/server"
	"logmerge/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	dataPath := flag.String("data", "data/entries.jsonl", "path to the JSONL store file")
	maxBytes := flag.Int("max-bytes", 64*1024, "max bytes per assembled record (0 = unlimited)")
	flushTimeout := flag.Duration("flush-timeout", 5*time.Second, "idle time after which a pending record is flushed as incomplete")
	startPattern := flag.String("start-pattern", `^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}`, "regexp marking the first line of a new record")
	flag.Parse()

	re, err := regexp.Compile(*startPattern)
	if err != nil {
		log.Fatalf("invalid -start-pattern: %v", err)
	}

	st, err := store.Open(*dataPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}

	m := merger.New(merger.Config{
		StartPattern: re,
		MaxBytes:     *maxBytes,
		FlushTimeout: *flushTimeout,
	}, func(e merger.Entry) {
		if err := st.Append(e); err != nil {
			log.Printf("store append failed: %v", err)
		}
	})

	srv := server.New(m, st)
	httpSrv := &http.Server{Addr: *addr, Handler: srv.Handler()}

	// Timeout sweeper: flush idle pending records.
	stop := make(chan struct{})
	go func() {
		interval := *flushTimeout / 2
		if interval <= 0 {
			interval = time.Second
		}
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-stop:
				return
			case now := <-t.C:
				if *flushTimeout > 0 {
					m.FlushStale(now.Add(-*flushTimeout))
				}
			}
		}
	}()

	go func() {
		log.Printf("listening on %s, store=%s", *addr, *dataPath)
		if err := httpSrv.ListenAndServe(); err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("shutting down: flushing pending records")
	close(stop)
	m.FlushAll(merger.ReasonShutdown)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	httpSrv.Shutdown(ctx)
}
