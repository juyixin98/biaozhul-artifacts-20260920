// Command chmig runs the consistent-hash migration simulator from a JSON
// configuration file and prints the JSON report.
//
// Usage:
//
//	chmig -in examples/scaleout.json [-out report.json]
//	chmig -in -            # read config from stdin
//
// Exit code is 0 when the process completed, even if a correctness check
// failed (failures are part of the report). Use -strict to exit non-zero when
// any verification fails, which is what CI uses.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"chmig/sim"
)

func main() {
	in := flag.String("in", "", "path to config JSON (- for stdin)")
	out := flag.String("out", "", "path to write report JSON (default stdout)")
	strict := flag.Bool("strict", false, "exit 2 if any verification fails")
	flag.Parse()

	if *in == "" {
		fmt.Fprintln(os.Stderr, "missing -in")
		os.Exit(1)
	}
	var rc io.ReadCloser
	if *in == "-" {
		rc = os.Stdin
	} else {
		f, err := os.Open(*in)
		if err != nil {
			fmt.Fprintf(os.Stderr, "open config: %v\n", err)
			os.Exit(1)
		}
		rc = f
	}
	defer rc.Close()

	var cfg sim.Config
	dec := json.NewDecoder(rc)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		fmt.Fprintf(os.Stderr, "decode config: %v\n", err)
		os.Exit(1)
	}
	rep, err := sim.Run(cfg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "simulation: %v\n", err)
		os.Exit(1)
	}
	b, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal report: %v\n", err)
		os.Exit(1)
	}
	b = append(b, '\n')
	if *out == "" || *out == "-" {
		os.Stdout.Write(b)
	} else {
		if err := os.WriteFile(*out, b, 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "write report: %v\n", err)
			os.Exit(1)
		}
	}
	if *strict && !allPassed(rep) {
		os.Exit(2)
	}
}

func allPassed(r *sim.Report) bool {
	v := r.Verifications
	checks := []sim.CheckResult{v.Ownership, v.Version, v.ConfirmedWritesSurvive,
		v.RemovalSafety, v.NoStaleReadAccepted}
	if v.Determinism != nil {
		checks = append(checks, *v.Determinism)
	}
	for _, c := range checks {
		if !c.Pass {
			return false
		}
	}
	return true
}
