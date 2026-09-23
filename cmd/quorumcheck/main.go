// Command quorumcheck validates weighted read/write quorum configurations and
// drives an in-process discrete-event simulator through a JSON interface.
//
// Usage:
//
//	quorumcheck [analyze|simulate|all] <request.json
//
// With no argument the action is taken from the request ("all" by default).
// Output is pretty-printed JSON on stdout. Exit codes: 0 ok (an unsafe
// configuration is still a successful run), 2 invalid request, 1 usage/IO
// error.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"quorumcheck/internal/quorum"
	"quorumcheck/internal/sim"
)

type request struct {
	Action string        `json:"action,omitempty"`
	Config quorum.Config `json:"config"`
	Sim    *sim.Sim      `json:"sim,omitempty"`
}

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		// Invalid requests are distinguished from tool failures.
		if ce, ok := err.(*clientError); ok {
			writeJSON(os.Stdout, map[string]any{"ok": false, "errors": ce.errors})
			os.Exit(2)
		}
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

type clientError struct {
	errors []string
}

func (e *clientError) Error() string { return fmt.Sprintf("invalid request: %v", e.errors) }

func run(args []string, in io.Reader, out io.Writer) error {
	action := ""
	if len(args) > 0 {
		action = args[0]
		switch action {
		case "analyze", "simulate", "all":
		default:
			return fmt.Errorf("unknown action %q (want analyze, simulate or all)", action)
		}
	}

	raw, err := io.ReadAll(in)
	if err != nil {
		return err
	}
	var req request
	if err := json.Unmarshal(raw, &req); err != nil {
		return &clientError{errors: []string{fmt.Sprintf("invalid JSON: %v", err)}}
	}
	if action == "" {
		if req.Action != "" {
			action = req.Action
		} else {
			action = "all"
		}
	}

	// Every action needs a valid config.
	report := quorum.Analyze(req.Config)
	if !report.Valid {
		return &clientError{errors: report.Errors}
	}

	result := map[string]any{
		"ok":     true,
		"action": action,
		"report": report,
	}

	if action == "simulate" {
		if req.Sim == nil {
			return &clientError{errors: []string{"simulate action requires a \"sim\" section"}}
		}
		simRep := sim.Run(toSimInput(req.Config, req.Sim))
		result["simulation"] = simRep
		writeJSON(out, result)
		return nil
	}

	attachWitnesses(report, req.Config)

	if action == "all" && req.Sim != nil {
		result["simulation"] = sim.Run(toSimInput(req.Config, req.Sim))
	} else if action == "all" {
		// Default stochastic characterisation: perfect network must complete
		// every operation; it is a cheap sanity cross-check of the analyzer.
		def := &sim.Sim{
			Seed: 42, Runs: 8, Kind: "mixed", Operations: 4,
			Horizon: 100, ClientTimeout: 15, MaxAttempts: 6,
			Network: &sim.Network{LossRate: 0, DuplicateRate: 0, MinDelay: 1, MaxDelay: 2},
		}
		result["simulation"] = sim.Run(toSimInput(req.Config, def))
	}

	writeJSON(out, result)
	return nil
}

// attachWitnesses replays every analyzer counterexample in the simulator and
// stores the scripted input and run result on the counterexample.
func attachWitnesses(report *quorum.Report, cfg quorum.Config) {
	nodes := make([]sim.SimNode, len(cfg.Nodes))
	for i, n := range cfg.Nodes {
		nodes[i] = sim.SimNode{ID: n.ID, Weight: n.Weight, Domain: n.Domain}
	}
	attach := func(ce *quorum.Counterexample) {
		if ce == nil || ce.Witness != nil {
			return
		}
		in := sim.SafetyWitness(nodes, cfg.ReadQuorum, cfg.WriteQuorum, ce.Kind, ce.QuorumA, ce.QuorumB, ce.FailedDomains)
		rep := sim.Run(in)
		ce.Witness = map[string]any{"input": in, "result": rep}
	}
	attach(report.MinimalWWCounterexample)
	attach(report.MinimalRWCounterexample)

	if !report.Available && len(report.MinimalAvailabilityFailure) > 0 {
		in := sim.AvailabilityWitness(nodes, cfg.ReadQuorum, cfg.WriteQuorum, "ww", report.MinimalAvailabilityFailure)
		rep := sim.Run(in)
		report.AvailabilityWitness = map[string]any{"input": in, "result": rep}
	}
}

func toSimInput(cfg quorum.Config, s *sim.Sim) sim.RunInput {
	nodes := make([]sim.SimNode, len(cfg.Nodes))
	for i, n := range cfg.Nodes {
		nodes[i] = sim.SimNode{ID: n.ID, Weight: n.Weight, Domain: n.Domain}
	}
	return sim.RunInput{Nodes: nodes, ReadQuorum: cfg.ReadQuorum, WriteQuorum: cfg.WriteQuorum, Sim: *s}
}

func writeJSON(w io.Writer, v any) {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fmt.Fprintln(os.Stderr, "error encoding output:", err)
	}
}
