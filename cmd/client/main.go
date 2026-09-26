// Command client is the fault-injection runner: it executes the acceptance
// scenarios against the in-process three-layer fixture and prints structured
// JSON results. Exit code is 1 when any scenario fails.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/example/retrybudget/internal/scenario"
)

func main() {
	pretty := flag.Bool("pretty", true, "indent JSON output")
	flag.Parse()

	results := scenario.RunAll()
	enc := json.NewEncoder(os.Stdout)
	if *pretty {
		enc.SetIndent("", "  ")
	}
	if err := enc.Encode(results); err != nil {
		fmt.Fprintln(os.Stderr, "encode:", err)
		os.Exit(2)
	}
	failed := 0
	for _, r := range results {
		if !r.Passed {
			failed++
		}
	}
	fmt.Fprintf(os.Stderr, "scenarios: %d, failed: %d\n", len(results), failed)
	if failed > 0 {
		os.Exit(1)
	}
}
