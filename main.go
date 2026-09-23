// Command twopc-sim runs a 2PC crash-recovery scenario from a JSON file and
// prints the judged result (including full event trace and blocking
// evidence) as JSON.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"twopc-sim/internal/harness"
)

func main() {
	outPath := flag.String("out", "", "write full JSON result to this path (stdout still gets a summary)")
	quiet := flag.Bool("quiet", false, "do not print the full JSON to stdout")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "usage: %s [--out file] [--quiet] <scenario.json>\n", os.Args[0])
		flag.PrintDefaults()
	}
	flag.Parse()
	if flag.NArg() != 1 {
		flag.Usage()
		os.Exit(2)
	}
	spec, err := harness.LoadSpec(flag.Arg(0))
	if err != nil {
		fmt.Fprintln(os.Stderr, "load scenario:", err)
		os.Exit(1)
	}
	res, err := harness.Run(spec)
	if err != nil {
		fmt.Fprintln(os.Stderr, "run:", err)
		os.Exit(1)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if !*quiet {
		if err := enc.Encode(res); err != nil {
			fmt.Fprintln(os.Stderr, "encode:", err)
			os.Exit(1)
		}
	}
	if *outPath != "" {
		f, err := os.Create(*outPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, "create out:", err)
			os.Exit(1)
		}
		oe := json.NewEncoder(f)
		oe.SetIndent("", "  ")
		if err := oe.Encode(res); err != nil {
			fmt.Fprintln(os.Stderr, "write out:", err)
			os.Exit(1)
		}
		_ = f.Close()
	}

	// Human-readable summary to stderr (exit code is always 0 for a clean
	// simulation; invariant violations are data inside the result).
	fmt.Fprintf(os.Stderr, "\n=== summary ===\n")
	for _, t := range res.Transactions {
		fmt.Fprintf(os.Stderr, "txn %-4s status=%-9s coordUp=%-5v client=%s\n",
			t.TxnID, t.Status, t.CoordinatorUp, orDash(t.ClientReported))
	}
	for _, b := range res.Blocked {
		fmt.Fprintf(os.Stderr, "BLOCKED txn %s prepared=%v coordUp=%v queries=%d\n  reason: %s\n",
			b.TxnID, b.PreparedNodes, b.CoordinatorUp, b.QueryCount, b.Reason)
	}
	if len(res.InvariantErrors) == 0 {
		fmt.Fprintln(os.Stderr, "invariants: OK (no partial commit; decision durable before send)")
	} else {
		for _, e := range res.InvariantErrors {
			fmt.Fprintf(os.Stderr, "INVARIANT VIOLATION: %s\n", e)
		}
	}
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
