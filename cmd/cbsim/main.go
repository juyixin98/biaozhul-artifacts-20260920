// Command cbsim runs one causal-broadcast simulation from a JSON request.
//
// Usage:
//
//	cbsim -in request.json [-out response.json]
//	cat request.json | cbsim
//
// With no -in the request is read from stdin; with no -out the response is
// written to stdout. Exit code is non-zero only for transport-level failures
// (unreadable/invalid JSON); a simulation-level validation error is reported
// with status="error" in the JSON response and exit code 0.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"causal-broadcast/internal/engine"
)

func main() {
	inPath := flag.String("in", "", "path to request JSON (default stdin)")
	outPath := flag.String("out", "", "path to response JSON (default stdout)")
	pretty := flag.Bool("pretty", true, "indent JSON output")
	flag.Parse()

	var in io.Reader = os.Stdin
	if *inPath != "" {
		f, err := os.Open(*inPath)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		in = f
	}

	var req engine.Request
	dec := json.NewDecoder(in)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		fatal(fmt.Errorf("invalid request JSON: %w", err))
	}

	resp := engine.Run(&req)

	var out io.Writer = os.Stdout
	if *outPath != "" {
		f, err := os.Create(*outPath)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		out = f
	}
	enc := json.NewEncoder(out)
	if *pretty {
		enc.SetIndent("", "  ")
	}
	if err := enc.Encode(resp); err != nil {
		fatal(err)
	}
	if *outPath != "" {
		fmt.Fprintf(os.Stderr, "response written to %s (status=%s)\n", *outPath, resp.Status)
	}
	if resp.Status == "error" {
		os.Exit(2)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "cbsim:", err)
	os.Exit(1)
}
