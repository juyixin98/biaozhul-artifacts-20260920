// Command verify runs the acceptance suite from the command line:
//
//   - enumerates short fault traces on the correct implementation and asserts
//     that no safety invariant is violated;
//   - runs the deterministic Figure 8 scenario on both the correct and the
//     deliberately buggy implementation, demonstrating the counterexample,
//     replaying it from JSON, and shrinking it;
//   - reports a human-readable summary and exits non-zero if the acceptance
//     criteria fail.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"raftlab/internal/check"
)

func main() {
	depth := flag.Int("depth", 3, "enumeration depth")
	maxTraces := flag.Int("max-traces", 4000, "enumeration trace cap")
	jsonOut := flag.Bool("json", false, "emit a machine-readable report")
	flag.Parse()

	report := acceptanceReport{
		OK:      true,
		Figure8: map[string]any{},
	}

	// 1. Enumeration over short fault traces: correct Raft must hold.
	enum := check.Enumerate(check.EnumerationConfig{
		Cluster:      check.Default3NodeConfig(),
		MaxDepth:     *depth,
		MaxTraces:    *maxTraces,
		TicksPerStep: 4,
	})
	report.Enumeration = enum
	if len(enum.Violations) != 0 {
		report.OK = false
		report.Failures = append(report.Failures,
			fmt.Sprintf("correct implementation produced %d violations", len(enum.Violations)))
	}

	// 2. Figure 8, correct variant.
	correctActions, err := check.Figure8Actions(false)
	must(err)
	_, correctRes := check.NewRunner(check.Figure8Config(false)).Run(correctActions)
	report.Figure8["correct"] = map[string]any{
		"traceLength": len(correctActions),
		"ok":          correctRes.OK(),
		"result":      correctRes,
	}
	if !correctRes.OK() {
		report.OK = false
		report.Failures = append(report.Failures, "correct raft failed the figure-8 scenario: "+correctRes.Violation.Message)
	}

	// 3. Figure 8, buggy variant: must produce a replayable conflict.
	buggyActions, err := check.Figure8Actions(true)
	must(err)
	_, buggyRes := check.NewRunner(check.Figure8Config(true)).Run(buggyActions)
	report.Figure8["buggy"] = map[string]any{
		"traceLength": len(buggyActions),
		"ok":          buggyRes.OK(),
		"result":      buggyRes,
	}
	if buggyRes.OK() {
		report.OK = false
		report.Failures = append(report.Failures, "the buggy implementation unexpectedly satisfied safety (scenario lost its bite)")
	} else {
		// 4. Counterexample replay from serialized JSON must reproduce.
		raw, _ := json.Marshal(buggyActions)
		var replay []check.Action
		must(json.Unmarshal(raw, &replay))
		_, replayRes := check.NewRunner(check.Figure8Config(true)).Run(replay)
		report.Figure8["replay"] = map[string]any{
			"ok":              !replayRes.OK(),
			"reproducedKind":  replayRes.Violation.Kind,
			"reproducedIndex": replayRes.Violation.Index,
		}
		if replayRes.OK() || replayRes.Violation.Kind != buggyRes.Violation.Kind ||
			replayRes.Violation.Index != buggyRes.Violation.Index {
			report.OK = false
			report.Failures = append(report.Failures, "counterexample did not reproduce on JSON replay")
		}

		// 5. Shrink the counterexample.
		shrunk := check.Shrink(buggyActions, buggyRes.Violation.Kind, check.Figure8Config(true))
		_, shrunkRes := check.NewRunner(check.Figure8Config(true)).Run(shrunk)
		shrunkOK := !shrunkRes.OK() && shrunkRes.Violation.Kind == buggyRes.Violation.Kind
		report.Figure8["shrunk"] = map[string]any{
			"ok":          shrunkOK,
			"originalLen": len(buggyActions),
			"shrunkLen":   len(shrunk),
			"trace":       shrunk,
		}
		if !shrunkOK {
			report.OK = false
			report.Failures = append(report.Failures, "shrunk trace no longer violates the invariant")
		}
	}

	if *jsonOut {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		must(enc.Encode(report))
	} else {
		printHuman(report, enum, correctRes, buggyRes)
	}
	if !report.OK {
		os.Exit(1)
	}
}

type acceptanceReport struct {
	OK          bool           `json:"ok"`
	Enumeration any            `json:"enumeration"`
	Figure8     map[string]any `json:"figure8"`
	Failures    []string       `json:"failures,omitempty"`
}

func printHuman(r acceptanceReport, enum check.EnumerationReport, correct, buggy check.TraceResult) {
	fmt.Println("Raft lab — acceptance run")
	fmt.Println("=========================")
	fmt.Printf("enumeration: %d traces, %d distinct states, exhausted=%v, violations=%d\n",
		enum.TracesRun, enum.StatesSeen, enum.Exhausted, len(enum.Violations))
	fmt.Printf("figure-8 correct implementation: ok=%v (final tick %d, leader %d)\n",
		correct.OK(), correct.FinalTick, correct.Leader)
	if buggy.OK() {
		fmt.Println("figure-8 buggy implementation: ok=true (UNEXPECTED)")
	} else {
		fmt.Printf("figure-8 buggy implementation: counterexample found\n")
		fmt.Printf("  violation: %s @ tick %d index %d\n",
			buggy.Violation.Kind, buggy.Violation.Tick, buggy.Violation.Index)
		fmt.Printf("  detail: %s\n", buggy.Violation.Message)
	}
	if f8, ok := r.Figure8["replay"].(map[string]any); ok {
		fmt.Printf("counterexample JSON replay: ok=%v kind=%v index=%v\n",
			f8["ok"], f8["reproducedKind"], f8["reproducedIndex"])
	}
	if f8, ok := r.Figure8["shrunk"].(map[string]any); ok {
		fmt.Printf("counterexample shrink: ok=%v (%v -> %v actions)\n",
			f8["ok"], f8["originalLen"], f8["shrunkLen"])
	}
	if len(r.Failures) == 0 {
		fmt.Println("\nRESULT: PASS — all acceptance criteria met")
	} else {
		fmt.Println("\nRESULT: FAIL")
		for _, f := range r.Failures {
			fmt.Println("  - " + f)
		}
	}
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(2)
	}
}
