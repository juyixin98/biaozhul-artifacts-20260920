// Command selftest brings up the service twice on loopback (identity and
// gzip), runs the acceptance scenarios over real HTTP with a fault-injecting
// client, and emits structured JSON plus a human summary. No production
// system is contacted; every dependency is an in-process fake.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"httprange/internal/report"
)

func main() {
	out := flag.String("out", "", "write structured JSON report to this path (default: stdout + .json)")
	flag.Parse()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	e, err := setupEnv(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "setup failed: %v\n", err)
		os.Exit(2)
	}
	defer e.shutdown()

	suite := runScenarios(e)

	pass, fail, skip := suite.Summary()
	printHuman(suite)

	path := *out
	if path == "" {
		path = "selftest-report.json"
	}
	if werr := writeReport(path, suite); werr != nil {
		fmt.Fprintf(os.Stderr, "warning: cannot write report: %v\n", werr)
	}

	fmt.Printf("\nreport written to %s\n", path)
	fmt.Printf("TOTAL %d  PASS %d  FAIL %d  SKIP %d\n",
		len(suite.Cases), pass, fail, skip)
	if fail > 0 {
		os.Exit(1)
	}
}

func runScenarios(e *env) report.Suite {
	start := time.Now().UTC()
	suite := report.Suite{
		Name:      "http-range-semantics acceptance",
		StartedAt: start,
	}
	for _, sc := range scenarios() {
		caseStart := time.Now()
		resp, err := sc.run(e)
		elapsed := time.Since(caseStart)

		c := report.Case{
			ID:        sc.id,
			Name:      sc.name,
			Given:     sc.given,
			When:      sc.when,
			Then:      sc.then,
			Request:   report.RequestSpec{Method: "GET", Path: "/artifacts/<id>"},
			Response:  resp,
			ElapsedMS: elapsed.Milliseconds(),
		}
		if err != nil {
			c.Status = report.StatusFail
			c.Detail = err.Error()
		} else {
			c.Status = report.StatusPass
		}
		suite.Cases = append(suite.Cases, c)
	}
	suite.DurationMS = time.Since(start).Milliseconds()
	return suite
}

func printHuman(s report.Suite) {
	fmt.Printf("== %s ==\n", s.Name)
	for _, c := range s.Cases {
		mark := "PASS"
		if c.Status == report.StatusFail {
			mark = "FAIL"
		} else if c.Status == report.StatusSkip {
			mark = "SKIP"
		}
		line := fmt.Sprintf("[%s] %-4s %d  %s", mark, c.ID, c.Response.StatusCode, c.Name)
		fmt.Println(line)
		if c.Status == report.StatusFail {
			fmt.Printf("        reason: %s\n", c.Detail)
		}
	}
}

func writeReport(path string, s report.Suite) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return report.WriteJSON(f, s)
}
