// Command sim runs a fencing-lease scenario from a JSON file and prints the
// deterministic run report as JSON.
//
// Usage:
//
//	sim -f scenario.json            # full report (state + invariants + trace)
//	sim -f scenario.json -summary   # omit the per-event trace
//	sim -f scenario.json -o out.json
//	sim -f -                        # read scenario JSON from stdin
//
// Exit code is 0 when all invariants pass, 1 when an invariant fails (a bug
// in the protocol/implementation), and 2 when the scenario itself is invalid.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"fencingleasesim/internal/sim"
)

func main() {
	file := flag.String("f", "", "scenario JSON file (\"-\" for stdin)")
	out := flag.String("o", "", "write report JSON to this file instead of stdout")
	summary := flag.Bool("summary", false, "omit the event trace from the report")
	flag.Parse()

	if *file == "" {
		fmt.Fprintln(os.Stderr, "usage: sim -f scenario.json [-summary] [-o report.json]")
		os.Exit(2)
	}

	var data []byte
	var err error
	if *file == "-" {
		data, err = io.ReadAll(os.Stdin)
	} else {
		data, err = os.ReadFile(*file)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "read scenario: %v\n", err)
		os.Exit(2)
	}

	res, err := sim.RunJSON(data)
	if err != nil {
		fmt.Fprintf(os.Stderr, "scenario error: %v\n", err)
		os.Exit(2)
	}

	if *summary {
		res.Report.Trace = nil
	}
	buf, err := json.MarshalIndent(res.Report, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "encode report: %v\n", err)
		os.Exit(2)
	}
	buf = append(buf, '\n')

	if *out != "" {
		if err := os.WriteFile(*out, buf, 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "write report: %v\n", err)
			os.Exit(2)
		}
	} else {
		os.Stdout.Write(buf)
	}

	fmt.Fprintf(os.Stderr, "scenario=%-28s commits=%d rejected=%d grants=%d invariants=%v\n",
		res.Report.Scenario,
		len(res.Report.Resource.Committed),
		len(res.Report.Resource.Rejected),
		res.Report.Lock.Grants,
		res.Report.AllInvariants)

	if !res.Report.AllInvariants {
		os.Exit(1)
	}
}
