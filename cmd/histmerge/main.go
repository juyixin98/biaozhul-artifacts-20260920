// Command histmerge runs the histogram ingest/query backend or seeds it with
// synthetic cumulative-histogram data.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"histmerge/internal/server"
	"histmerge/internal/store"
	"histmerge/internal/synthetic"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "seed" {
		runSeed(os.Args[2:])
		return
	}
	runServe(os.Args[1:])
}

func runServe(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", ":8080", "listen address")
	wal := fs.String("wal", "data/histmerge.wal.jsonl", "WAL path (empty = memory only)")
	_ = fs.Parse(args)

	st, err := store.New(*wal)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	srv := server.New(st)
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           withLogging(srv.Mux),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		log.Printf("histmerge listening on %s (wal=%q)", *addr, *wal)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")
	if err := httpSrv.Close(); err != nil {
		log.Printf("http close: %v", err)
	}
	if err := st.Close(); err != nil {
		log.Printf("store close: %v", err)
	}
}

func withLogging(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rw := &statusRW{ResponseWriter: w, status: 200}
		h.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d", r.Method, r.URL.RequestURI(), rw.status)
	})
}

type statusRW struct {
	http.ResponseWriter
	status int
}

func (s *statusRW) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func runSeed(args []string) {
	fs := flag.NewFlagSet("seed", flag.ExitOnError)
	addr := fs.String("addr", "http://127.0.0.1:8080", "server base URL")
	scrapes := fs.Int("scrapes", 6, "number of cumulative scrape snapshots per series")
	seed := fs.Int64("seed", 42, "PRNG seed")
	includeEmpty := fs.Bool("empty", true, "also ingest one valid empty histogram")
	_ = fs.Parse(args)

	base := time.Now().UTC().Add(-time.Duration(*scrapes) * time.Minute)
	samples, _ := synthetic.Generate(base, *scrapes, *seed)
	if *includeEmpty {
		samples = append(samples, synthetic.EmptySample(base, "idle-svc"))
	}
	body, err := json.Marshal(map[string]any{"samples": samples})
	if err != nil {
		log.Fatal(err)
	}
	resp, err := http.Post(*addr+"/api/v1/ingest", "application/json", bytes.NewReader(body))
	if err != nil {
		log.Fatalf("seed POST failed (is the server running?): %v", err)
	}
	defer resp.Body.Close()
	fmt.Printf("seeded %d samples; ingest status %s\n", len(samples), resp.Status)
	raw, _ := io.ReadAll(resp.Body)
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, raw, "", "  "); err == nil {
		fmt.Println(pretty.String())
	}
}
