// Command orset runs deterministic OR-Set discrete-event simulations from
// JSON config files.
//
//	orset run <config.json> [-o report.json] [--trace]
//	orset serve [:8080]            # optional HTTP JSON interface
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"

	"orsetsim/internal/sim"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "run":
		runCmd(os.Args[2:])
	case "serve":
		serveCmd(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n", os.Args[1])
		usage()
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `orset — deterministic OR-Set discrete-event simulator

usage:
  orset run <config.json> [-o report.json] [--trace]
  orset serve [addr]             addr default :8080

config JSON:
  {
    "seed": 42,
    "nodes": ["n1", "n2", "n3"],
    "network": {"loss_prob":0.2, "duplicate_prob":0.2, "reorder_prob":0.5,
                "min_delay":1, "max_delay":3},
    "events": [
      {"time":1, "node":"n1", "op":"add", "element":"a"},
      {"time":2, "node":"n1", "op":"sync"},
      {"time":5, "node":"n2", "op":"remove", "element":"a"},
      {"time":6, "node":"n2", "op":"sync"},
      {"time":20, "node":"n1", "op":"gc"}
    ],
    "max_ticks": 0
  }

POST /simulate accepts the same JSON body and returns the run report.
`)
	os.Exit(2)
}

func runCmd(args []string) {
	// Permute argv so flags precede the positional config path; the stdlib
	// flag package stops parsing at the first non-flag argument.
	var flags, positional []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "-o" && i+1 < len(args) {
			flags = append(flags, a, args[i+1])
			i++
			continue
		}
		if strings.HasPrefix(a, "-") {
			flags = append(flags, a)
			continue
		}
		positional = append(positional, a)
	}
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	out := fs.String("o", "", "write full report to file (default: stdout)")
	showTrace := fs.Bool("trace", false, "print the event trace (stderr, after run)")
	if err := fs.Parse(flags); err != nil {
		log.Fatal(err)
	}
	if len(positional) != 1 {
		log.Fatal("run requires exactly one config file path")
	}
	raw, err := os.ReadFile(positional[0])
	if err != nil {
		log.Fatalf("read config: %v", err)
	}
	rep, err := execute(raw)
	if err != nil {
		log.Fatalf("simulation error: %v", err)
	}
	body, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		log.Fatal(err)
	}
	body = append(body, '\n')
	if *out != "" {
		if err := os.WriteFile(*out, body, 0o644); err != nil {
			log.Fatalf("write report: %v", err)
		}
		fmt.Printf("report written to %s\n", *out)
	} else {
		os.Stdout.Write(body)
	}
	printSummary(rep)
	if *showTrace {
		fmt.Fprintln(os.Stderr, "--- trace ---")
		for _, t := range rep.Trace {
			fmt.Fprintf(os.Stderr, "t=%-4d %-7s %s\n", t.Time, t.Kind, traceLine(t))
		}
	}
}

func traceLine(t sim.Entry) string {
	switch t.Kind {
	case "add":
		return fmt.Sprintf("%s ADD %s tag=%s values=%v", t.Node, t.Element, t.Tags, t.Values)
	case "remove":
		return fmt.Sprintf("%s REMOVE %s tags=%v values=%v", t.Node, t.Element, t.Tags, t.Values)
	case "send":
		return fmt.Sprintf("%s -> %s (msg %d)", t.From, t.Target, t.MsgID)
	case "drop":
		return fmt.Sprintf("%s -> %s (msg %d) DROPPED", t.From, t.Target, t.MsgID)
	case "dup":
		return fmt.Sprintf("%s -> %s (msg %d) %s", t.From, t.Target, t.MsgID, t.Detail)
	case "deliver":
		return fmt.Sprintf("%s <- %s (msg %d) values=%v [%s]", t.Node, t.From, t.MsgID, t.Values, t.Detail)
	case "gc":
		return fmt.Sprintf("GC %s", t.Detail)
	default:
		return t.Detail
	}
}

func printSummary(r *sim.Report) {
	fmt.Fprintf(os.Stderr, "converged=%v equal=%v final=%v ticks=%d counts=%v\n",
		r.Converged, r.AllStatesEqual, r.FinalValues, r.Ticks, r.Counts)
}

func execute(raw []byte) (*sim.Report, error) {
	var cfg sim.Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("invalid config JSON: %w", err)
	}
	eng, err := sim.NewEngine(cfg)
	if err != nil {
		return nil, err
	}
	return eng.Run(), nil
}

func serveCmd(args []string) {
	addr := ":8080"
	if len(args) > 0 {
		addr = args[0]
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, "ok\n")
	})
	mux.HandleFunc("/simulate", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, 4<<20))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		rep, err := execute(body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(rep)
	})
	log.Printf("orset simulator listening on %s (POST /simulate)", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
