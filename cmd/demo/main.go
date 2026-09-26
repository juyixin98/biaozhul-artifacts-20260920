// Command cbhalfopen-demo runs either the local HTTP service (default) or the
// built-in virtual-clock scenarios that emit structured JSON reports.
//
// Usage:
//
//	cbhalfopen-demo serve [--addr :8080]
//	cbhalfopen-demo scenarios [--json] [--name <scenario>]
//
// Nothing here talks to a real external system: the "upstream" is an
// in-process fake driven entirely by the API and the virtual clock.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/demoapp"
	"cbhalfopen/internal/faultclient"
	"cbhalfopen/internal/scenario"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		args = []string{"serve"}
	}
	switch args[0] {
	case "serve":
		return cmdServe(args[1:])
	case "scenarios":
		return cmdScenarios(args[1:])
	case "-h", "--help", "help":
		printUsage()
		return nil
	default:
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

func printUsage() {
	fmt.Fprint(os.Stderr, `cbhalfopen-demo - circuit breaker half-open race playground

  serve [--addr :8080] [--cooldown 5s] [--probes 3] [--required 2]
                        Start the local HTTP service with an in-process fake
  scenarios [--json] [--name NAME]
                        Run virtual-clock acceptance scenarios
`)
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", ":8080", "listen address")
	cooldown := fs.Duration("cooldown", 5*time.Second, "breaker open cooldown")
	maxProbes := fs.Int("probes", 3, "max concurrent half-open probes")
	required := fs.Int("required", 2, "successful probes required to close")
	window := fs.Int("window", 10, "sliding window sample size")
	minReq := fs.Int("min-requests", 5, "minimum samples before tripping")
	threshold := fs.Float64("threshold", 0.5, "failure ratio that opens the breaker")
	if err := fs.Parse(args); err != nil {
		return err
	}

	srv := demoapp.New(demoappConfig(*cooldown, *maxProbes, *required, *window, *minReq, *threshold), faultclient.Config{})
	fmt.Printf("cbhalfopen demo service listening on %s\n", *addr)
	fmt.Println("virtual clock starts at 1970-01-01T00:00:00Z; advance it with POST /clock/advance")
	fmt.Println("try: curl -s localhost:8080/state | jq")
	return http.ListenAndServe(*addr, srv.Handler())
}

func demoappConfig(cooldown time.Duration, maxProbes, required, window, minReq int, threshold float64) breaker.Config {
	return breaker.Config{
		SlidingWindowSize: window,
		MinRequests:       minReq,
		FailureThreshold:  threshold,
		OpenCooldown:      cooldown,
		HalfOpenMaxProbes: maxProbes,
		RequiredSuccesses: required,
	}
}

func cmdScenarios(args []string) error {
	fs := flag.NewFlagSet("scenarios", flag.ExitOnError)
	asJSON := fs.Bool("json", false, "emit a single JSON document")
	name := fs.String("name", "", "run only the named scenario")
	if err := fs.Parse(args); err != nil {
		return err
	}

	var reports []*scenario.Report
	for _, d := range scenario.All() {
		if *name != "" && d.Name != *name {
			continue
		}
		reports = append(reports, d.Run())
	}
	if len(reports) == 0 {
		return fmt.Errorf("no scenario matched name %q", *name)
	}

	allPass := true
	if *asJSON {
		out := map[string]any{
			"generated_at":   time.Now().UTC().Format(time.RFC3339),
			"scenario_count": len(reports),
			"scenarios":      reports,
		}
		for _, r := range reports {
			if !r.Pass {
				allPass = false
			}
		}
		out["all_pass"] = allPass
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			return err
		}
	} else {
		for _, r := range reports {
			printTextReport(r)
			if !r.Pass {
				allPass = false
			}
		}
		fmt.Println("------------------------------------------------------------")
		fmt.Printf("SCENARIOS: %d    PASS: %d    FAIL: %d\n",
			len(reports), countPass(reports), len(reports)-countPass(reports))
	}

	if !allPass {
		os.Exit(2)
	}
	return nil
}

func countPass(rs []*scenario.Report) int {
	n := 0
	for _, r := range rs {
		if r.Pass {
			n++
		}
	}
	return n
}

func printTextReport(r *scenario.Report) {
	status := "PASS"
	if !r.Pass {
		status = "FAIL"
	}
	fmt.Println("------------------------------------------------------------")
	fmt.Printf("[%s] %s\n", status, r.Name)
	fmt.Printf("goal: %s\n", r.Goal)
	for _, s := range r.Steps {
		fmt.Printf("  t=%s  step %d: %s\n", s.Time.UTC().Format("15:04:05.000"), s.Index, s.Name)
		if s.Detail != "" {
			fmt.Printf("          %s\n", s.Detail)
		}
		for _, c := range s.Checks {
			mark := "ok  "
			if !c.Pass {
				mark = "FAIL"
			}
			fmt.Printf("          [%s] %s (%s)\n", mark, c.Name, c.Detail)
		}
	}
	fmt.Printf("  counters: %v\n", r.Summary)
}
