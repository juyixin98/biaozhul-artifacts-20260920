// Command pim runs a simulation from a spec file (or a built-in scenario)
// and prints the full result JSON, including the scheduling timeline, to
// stdout.
//
// Usage:
//
//	pim -scenario classic-inversion-no-pi
//	pim -file ./examples/three-level.json
//
// Exit status is non-zero on invalid input; a detected deadlock is still a
// successful run (the deadlock is part of the result), unless -fail-deadlock
// is set (useful in scripts/tests).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"pim/internal/scenario"
	"pim/internal/scheduler"
)

func main() {
	scenarioName := flag.String("scenario", "", "name of a built-in scenario")
	file := flag.String("file", "", "path to a spec JSON file ('-' for stdin)")
	failDeadlock := flag.Bool("fail-deadlock", false, "exit non-zero when the run deadlocks")
	flag.Parse()

	var spec scheduler.Spec
	switch {
	case *scenarioName != "" && *file != "":
		fatal("-scenario and -file are mutually exclusive")
	case *scenarioName != "":
		build, ok := scenario.All()[*scenarioName]
		if !ok {
			fatal("unknown scenario %q", *scenarioName)
		}
		spec = build()
	case *file != "":
		data, err := readInput(*file)
		if err != nil {
			fatal("%v", err)
		}
		if err := json.Unmarshal(data, &spec); err != nil {
			fatal("parsing spec: %v", err)
		}
	default:
		fatal("one of -scenario or -file is required")
	}

	res, err := scheduler.Run(spec, nil, nil)
	if err != nil {
		fatal("%v", err)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(res); err != nil {
		fatal("%v", err)
	}
	if *failDeadlock && res.Deadlock != nil {
		os.Exit(2)
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

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "pim: "+format+"\n", args...)
	os.Exit(1)
}
