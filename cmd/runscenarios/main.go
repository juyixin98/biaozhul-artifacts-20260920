// Command runscenarios executes the built-in acceptance scenarios with the
// virtual clock and writes structured JSON reports (one file per scenario
// plus a summary), then exits non-zero if any scenario failed. No network, no
// production system: the downstream is an in-process fake.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"

	"breakerhalfopen/internal/scenario"
)

type summary struct {
	Total     int               `json:"total"`
	Passed    int               `json:"passed"`
	Failed    int               `json:"failed"`
	Scenarios []scenarioSummary `json:"scenarios"`
}

type scenarioSummary struct {
	Name     string   `json:"name"`
	Pass     bool     `json:"pass"`
	Steps    int      `json:"steps"`
	Failures []string `json:"failures,omitempty"`
}

func main() {
	outDir := flag.String("out", "reports", "directory for structured JSON reports")
	flag.Parse()

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		log.Fatalf("create output dir: %v", err)
	}

	sum := summary{}
	for _, fn := range scenario.All() {
		rep := fn()
		sum.Total++
		ss := scenarioSummary{Name: rep.Name, Pass: rep.Pass, Steps: len(rep.Steps), Failures: rep.Failures}
		if rep.Pass {
			sum.Passed++
		} else {
			sum.Failed++
		}
		sum.Scenarios = append(sum.Scenarios, ss)

		path := filepath.Join(*outDir, rep.Name+".json")
		if err := writeJSON(path, rep); err != nil {
			log.Fatalf("write %s: %v", path, err)
		}
		fmt.Printf("[%s] %-24s steps=%2d pass=%v\n", mark(rep.Pass), rep.Name, len(rep.Steps), rep.Pass)
		for _, f := range rep.Failures {
			fmt.Printf("        FAIL: %s\n", f)
		}
	}

	if err := writeJSON(filepath.Join(*outDir, "summary.json"), sum); err != nil {
		log.Fatalf("write summary: %v", err)
	}
	fmt.Printf("\n%d/%d scenarios passed; reports in %s/\n", sum.Passed, sum.Total, *outDir)

	if sum.Failed != 0 {
		os.Exit(1)
	}
}

func mark(ok bool) string {
	if ok {
		return "PASS"
	}
	return "FAIL"
}

func writeJSON(path string, v any) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}
