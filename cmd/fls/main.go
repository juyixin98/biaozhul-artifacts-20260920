// Command fls drives the fencing-lease simulator.
//
// Usage:
//
//	fls run <scenario.json> [--out result.json] [--quiet]
//	fls demo <name>   built-in: renew-lost, pause-expired, late-message,
//	                  duplicate, no-fence, fuzz
//	fls list
//
// Exit code is 0 only when every expectation and the built-in fence
// monotonicity invariant pass.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"fencinglease/internal/runner"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "run":
		os.Exit(cmdRun(os.Args[2:]))
	case "demo":
		os.Exit(cmdDemo(os.Args[2:]))
	case "demo-json":
		os.Exit(cmdDemoJSON(os.Args[2:]))
	case "list":
		listDemos()
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: fls run <scenario.json> [--out result.json] [--quiet]")
	fmt.Fprintln(os.Stderr, "       fls demo <renew-lost|pause-expired|late-message|duplicate|no-fence|fuzz>")
	fmt.Fprintln(os.Stderr, "       fls list")
}

func decodeScenario(path string) (*runner.Scenario, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return runner.Parse(raw, path)
}

func emit(res *runner.Result, outPath string, quiet bool) int {
	raw, err := json.MarshalIndent(res, "", "  ")
	if err != nil {
		panic(err)
	}
	if outPath != "" {
		if err := os.WriteFile(outPath, raw, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, "write result:", err)
			return 2
		}
	}
	if !quiet {
		fmt.Println(string(raw))
	}
	if !res.OK {
		fmt.Fprintf(os.Stderr, "FAIL: %s\n", res.Name)
		for _, c := range res.Checks {
			if !c.Pass {
				fmt.Fprintf(os.Stderr, "  - %s: %s\n", c.Kind, c.Detail)
			}
		}
		return 1
	}
	fmt.Fprintf(os.Stderr, "PASS: %s (%d checks)\n", res.Name, len(res.Checks))
	return 0
}

func cmdRun(args []string) int {
	var path, out string
	quiet := false
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "--quiet":
			quiet = true
		case a == "--out" && i+1 < len(args):
			out = args[i+1]
			i++
		case len(a) > 6 && a[:6] == "--out=":
			out = a[6:]
		case a[0] != '-':
			path = a
		}
	}
	if path == "" {
		usage()
		return 2
	}
	sc, err := decodeScenario(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	return emit(runner.Run(sc), out, quiet)
}

func cmdDemo(args []string) int {
	if len(args) == 0 {
		usage()
		return 2
	}
	name := args[0]
	if name == "fuzz" {
		return runFuzz(10)
	}
	builder, ok := demos[name]
	if !ok {
		fmt.Fprintf(os.Stderr, "unknown demo %q\n", name)
		listDemos()
		return 2
	}
	return emit(runner.Run(builder(1)), "", false)
}

func listDemos() {
	for _, n := range []string{"renew-lost", "pause-expired", "late-message", "duplicate", "no-fence", "fuzz"} {
		fmt.Println(n)
	}
}
