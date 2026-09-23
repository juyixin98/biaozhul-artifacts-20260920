// quorumsim is a pure-backend CLI for weighted quorum checking and
// deterministic quorum-protocol simulation.
//
// Usage:
//
//	quorumsim check <config.json|->   verify quorum intersection safety
//	quorumsim enum  <config.json|->   enumerate available sets & minimal quorums per failure scenario
//	quorumsim run   <run.json|->      run the discrete-event simulation
//
// Input is read from the named file, or stdin when "-". Output is JSON on
// stdout. Exit code: 0 = safe / no violations, 1 = unsafe / violations
// found, 2 = error.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"quorumcheck/internal/quorum"
	"quorumcheck/internal/sim"
)

func main() {
	if len(os.Args) < 3 {
		usage()
		os.Exit(2)
	}
	cmd, path := os.Args[1], os.Args[2]
	data, err := readInput(path)
	if err != nil {
		fatal(err)
	}
	switch cmd {
	case "check":
		os.Exit(cmdCheck(data))
	case "enum":
		os.Exit(cmdEnum(data))
	case "run":
		os.Exit(cmdRun(data))
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintf(os.Stderr, `usage:
  quorumsim check <config.json|->   verify read/write and write/write quorum intersection
  quorumsim enum  <config.json|->   enumerate available sets and minimal quorums per failure scenario
  quorumsim run   <run.json|->      run the deterministic discrete-event simulation
input from file, or stdin with "-". exit codes: 0 ok, 1 unsafe/violations, 2 error.
`)
}

func readInput(path string) ([]byte, error) {
	if path == "-" {
		return io.ReadAll(os.Stdin)
	}
	return os.ReadFile(path)
}

func fatal(err error) {
	fmt.Fprintf(os.Stderr, "error: %v\n", err)
	os.Exit(2)
}

func writeJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fatal(err)
	}
}

func cmdCheck(data []byte) int {
	var cfg quorum.Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		fatal(fmt.Errorf("invalid config JSON: %w", err))
	}
	rep, err := quorum.Check(&cfg)
	if err != nil {
		fatal(err)
	}
	writeJSON(rep)
	if rep.Safe {
		return 0
	}
	return 1
}

type enumOutput struct {
	TotalWeight     int                     `json:"total_weight"`
	Scenarios       []quorum.ScenarioReport `json:"scenarios"`
	MinReadQuorums  map[string][][]string   `json:"min_read_quorums"`
	MinWriteQuorums map[string][][]string   `json:"min_write_quorums"`
}

func cmdEnum(data []byte) int {
	var cfg quorum.Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		fatal(fmt.Errorf("invalid config JSON: %w", err))
	}
	rep, err := quorum.Check(&cfg)
	if err != nil {
		fatal(err)
	}
	rq, err := quorum.EnumerateQuorums(&cfg, cfg.ReadThreshold)
	if err != nil {
		fatal(err)
	}
	wq, err := quorum.EnumerateQuorums(&cfg, cfg.WriteThreshold)
	if err != nil {
		fatal(err)
	}
	writeJSON(enumOutput{
		TotalWeight:     rep.TotalWeight,
		Scenarios:       rep.Scenarios,
		MinReadQuorums:  rq,
		MinWriteQuorums: wq,
	})
	return 0
}

func cmdRun(data []byte) int {
	var spec sim.RunSpec
	if err := json.Unmarshal(data, &spec); err != nil {
		fatal(fmt.Errorf("invalid run JSON: %w", err))
	}
	res, err := sim.Run(&spec)
	if err != nil {
		fatal(err)
	}
	writeJSON(res)
	if len(res.Violations) > 0 {
		return 1
	}
	return 0
}
