// Command cpathtrace runs the trace critical-path analysis backend:
// HTTP ingestion/query over a local JSON-file store, plus a small CLI
// for offline analysis and synthetic seeding.
//
//	cpathtrace serve [--addr :8080] [--data ./data]
//	cpathtrace seed  [--data ./data] [--name all]
//	cpathtrace analyze --file path.json
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"cpathtrace/internal/analyzer"
	"cpathtrace/internal/api"
	"cpathtrace/internal/model"
	"cpathtrace/internal/store"
	"cpathtrace/internal/synthetic"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = cmdServe(os.Args[2:])
	case "seed":
		err = cmdSeed(os.Args[2:])
	case "analyze":
		err = cmdAnalyze(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		log.Fatalf("error: %v", err)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `usage:
  cpathtrace serve   [--addr :8080] [--data ./data]
  cpathtrace seed    [--data ./data] [--name all|serial_parallel|sync_overlap|missing_span|cycle|clock_skew]
  cpathtrace analyze --file trace.json
`)
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", ":8080", "listen address")
	dataDir := fs.String("data", "./data", "trace storage directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	st, err := store.NewFileStore(*dataDir)
	if err != nil {
		return err
	}
	srv := api.NewServer(st)
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           withLogging(srv.Mux),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("cpathtrace listening on %s, storing traces in %s", *addr, *dataDir)
	return httpSrv.ListenAndServe()
}

func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(sw, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), sw.status, time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func cmdSeed(args []string) error {
	fs := flag.NewFlagSet("seed", flag.ExitOnError)
	dataDir := fs.String("data", "./data", "trace storage directory")
	name := fs.String("name", "all", "scenario name or 'all'")
	if err := fs.Parse(args); err != nil {
		return err
	}
	st, err := store.NewFileStore(*dataDir)
	if err != nil {
		return err
	}
	names := synthetic.Names()
	if *name != "all" {
		names = []string{*name}
	}
	for _, n := range names {
		t, ok := synthetic.Build(n)
		if !ok {
			return fmt.Errorf("unknown scenario %q", n)
		}
		if err := st.Save(t); err != nil {
			return err
		}
		fmt.Printf("seeded %s as trace %s (%d spans)\n", n, t.TraceID, len(t.Spans))
	}
	return nil
}

func cmdAnalyze(args []string) error {
	fs := flag.NewFlagSet("analyze", flag.ExitOnError)
	file := fs.String("file", "", "path to a trace JSON file")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *file == "" {
		return fmt.Errorf("--file is required")
	}
	data, err := os.ReadFile(*file)
	if err != nil {
		return err
	}
	var t model.Trace
	if err := json.Unmarshal(data, &t); err != nil {
		return fmt.Errorf("parse trace: %w", err)
	}
	for i := range t.Spans {
		if err := t.Spans[i].Normalize(); err != nil {
			return err
		}
	}
	res := analyzer.Analyze(t)
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(res)
}
