// Command testsum summarizes a `go test -json` stream into a compact
// structured report: per-package pass/fail counts and failed test names.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"sort"
)

type event struct {
	Action  string `json:"Action"`
	Package string `json:"Package"`
	Test    string `json:"Test"`
}

type pkgResult struct {
	Passed   int      `json:"passed"`
	Failed   int      `json:"failed"`
	Skipped  int      `json:"skipped"`
	Failures []string `json:"failures,omitempty"`
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: testsum <test-results.json>")
		os.Exit(2)
	}
	f, err := os.Open(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	defer f.Close()

	pkgs := map[string]*pkgResult{}
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var ev event
		if json.Unmarshal(sc.Bytes(), &ev) != nil || ev.Test == "" {
			continue
		}
		r := pkgs[ev.Package]
		if r == nil {
			r = &pkgResult{}
			pkgs[ev.Package] = r
		}
		switch ev.Action {
		case "pass":
			r.Passed++
		case "fail":
			r.Failed++
			r.Failures = append(r.Failures, ev.Test)
		case "skip":
			r.Skipped++
		}
	}

	names := make([]string, 0, len(pkgs))
	for p := range pkgs {
		names = append(names, p)
	}
	sort.Strings(names)

	failed := false
	out := map[string]any{"packages": map[string]*pkgResult{}}
	for _, p := range names {
		r := pkgs[p]
		out["packages"].(map[string]*pkgResult)[p] = r
		status := "PASS"
		if r.Failed > 0 {
			status = "FAIL"
			failed = true
		}
		fmt.Printf("%s %s (passed=%d failed=%d skipped=%d)\n", status, p, r.Passed, r.Failed, r.Skipped)
		for _, name := range r.Failures {
			fmt.Printf("  FAILED: %s\n", name)
		}
	}
	summary, _ := json.MarshalIndent(out, "", "  ")
	if err := os.WriteFile("results/summary.json", summary, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	fmt.Println("structured summary written to results/summary.json")
	if failed {
		os.Exit(1)
	}
}
