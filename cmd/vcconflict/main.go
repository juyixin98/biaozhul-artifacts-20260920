// Command vcconflict runs a deterministic vector-clock conflict
// simulation scenario described in JSON and prints the JSON result.
//
// Usage:
//
//	vcconflict -f scenario.json [-o result.json]
//	cat scenario.json | vcconflict
package main

import (
	"flag"
	"fmt"
	"io"
	"os"

	"vcconflict/internal/sim"
)

func main() {
	file := flag.String("f", "", "path to the scenario JSON file (default: stdin)")
	out := flag.String("o", "", "path to write the result JSON (default: stdout)")
	flag.Parse()

	var input []byte
	var err error
	if *file != "" {
		input, err = os.ReadFile(*file)
	} else {
		input, err = io.ReadAll(os.Stdin)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "read scenario:", err)
		os.Exit(1)
	}

	result, err := sim.RunJSON(input)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}

	if *out != "" {
		if err := os.WriteFile(*out, append(result, '\n'), 0o644); err != nil {
			fmt.Fprintln(os.Stderr, "write result:", err)
			os.Exit(1)
		}
		return
	}
	fmt.Println(string(result))
}
