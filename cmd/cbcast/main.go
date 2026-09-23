// Command cbcast runs the causal-broadcast discrete-event simulator.
//
// Usage:
//
//	cbcast -in request.json [-out result.json]
//	cbcast -gen chain|concurrent [-nodes 4] [-seed 1] [-loss] [-out x.json]
//
// With -gen, the generated request itself is printed when -out is omitted;
// pass -run to execute it and print the result instead.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"causal-broadcast/internal/jsonio"
	"causal-broadcast/internal/scenario"
)

func main() {
	var (
		in    = flag.String("in", "", "path to a JSON run request (- for stdin)")
		out   = flag.String("out", "", "path to write JSON output (default: stdout)")
		gen   = flag.String("gen", "", "generate a built-in scenario: chain | concurrent")
		nodes = flag.Int("nodes", 4, "number of nodes for -gen")
		seed  = flag.Int64("seed", 1, "network RNG seed for -gen")
		loss  = flag.Bool("loss", false, "for chain: force-drop the penultimate message at the final node")
		dup   = flag.Bool("duplicate", true, "for concurrent: force a late duplicate copy")
		run   = flag.Bool("run", false, "with -gen: execute the generated scenario instead of printing it")
	)
	flag.Parse()

	if *gen != "" {
		req, err := generate(*gen, *nodes, *seed, *loss, *dup)
		if err != nil {
			fatal(err)
		}
		if !*run {
			writeJSON(mustMarshal(req), *out)
			return
		}
		raw, err := json.Marshal(req)
		if err != nil {
			fatal(err)
		}
		res, err := jsonio.Run(raw)
		if err != nil {
			fatal(err)
		}
		writeJSON(mustMarshal(res), *out)
		return
	}

	if *in == "" {
		flag.Usage()
		os.Exit(2)
	}
	raw, err := readInput(*in)
	if err != nil {
		fatal(err)
	}
	res, err := jsonio.Run(raw)
	if err != nil {
		fatal(err)
	}
	writeJSON(mustMarshal(res), *out)
}

func generate(kind string, n int, seed int64, loss, dup bool) (jsonio.Request, error) {
	switch kind {
	case "chain":
		return scenario.Chain(n, seed, loss), nil
	case "concurrent":
		return scenario.Concurrent(n, seed, dup), nil
	default:
		return jsonio.Request{}, fmt.Errorf("unknown -gen %q (want chain or concurrent)", kind)
	}
}

func readInput(path string) ([]byte, error) {
	if path == "-" {
		return readAll(os.Stdin)
	}
	return os.ReadFile(path)
}

func readAll(f *os.File) ([]byte, error) {
	return io.ReadAll(f)
}

func mustMarshal(v any) []byte {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fatal(err)
	}
	b = append(b, '\n')
	return b
}

func writeJSON(data []byte, path string) {
	if path == "" {
		if _, err := os.Stdout.Write(data); err != nil {
			fatal(err)
		}
		return
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "cbcast:", err)
	os.Exit(1)
}
