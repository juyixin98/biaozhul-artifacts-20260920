// Command vccsim runs the deterministic vector-clock conflict simulator.
//
// Usage:
//
//	vccsim run [-file scenario.json] [-out report.json]
//	           (reads stdin when -file is omitted; writes stdout when -out is omitted)
//	vccsim serve [-addr :8080]
//	           POST /run   {"name":...,"nodes":...,"events":...} -> report JSON
//	           GET  /health
//
// Everything is in-process: no real network, no goroutines per node, no
// cluster dependency.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"

	"vccsim/internal/sim"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "run":
		runCmd(os.Args[2:])
	case "serve":
		serveCmd(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `vccsim - deterministic vector-clock conflict simulator

Usage:
  vccsim run   [-file scenario.json] [-out report.json]
  vccsim serve [-addr :8080]

run:   execute one scenario JSON, emit one report JSON
serve: HTTP wrapper; POST a scenario to /run, get a report back
`)
}

func runCmd(args []string) {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	in := fs.String("file", "", "scenario JSON file (default: stdin)")
	out := fs.String("out", "", "report JSON file (default: stdout)")
	_ = fs.Parse(args)

	var r io.Reader = os.Stdin
	if *in != "" {
		f, err := os.Open(*in)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		r = f
	}
	sc, err := decodeScenario(r)
	if err != nil {
		fatal(err)
	}
	rep, err := sim.Run(sc)
	if err != nil {
		fatal(err)
	}
	if err := writeReport(rep, *out); err != nil {
		fatal(err)
	}
	// Exit non-zero when the scenario's own assertions failed.
	if !rep.AssertionsOK {
		os.Exit(1)
	}
}

func serveCmd(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", ":8080", "listen address")
	_ = fs.Parse(args)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})
	mux.HandleFunc("/run", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST required", http.StatusMethodNotAllowed)
			return
		}
		sc, err := decodeScenario(r.Body)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		rep, err := sim.Run(sc)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, rep)
	})
	log.Printf("vccsim listening on %s (POST /run)", *addr)
	log.Fatal(http.ListenAndServe(*addr, mux))
}

func decodeScenario(r io.Reader) (sim.Scenario, error) {
	var sc sim.Scenario
	dec := json.NewDecoder(r)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&sc); err != nil {
		return sc, fmt.Errorf("invalid scenario JSON: %w", err)
	}
	return sc, nil
}

func writeReport(rep *sim.Report, path string) error {
	data, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if path == "" {
		_, err = os.Stdout.Write(data)
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "vccsim:", err)
	os.Exit(1)
}
