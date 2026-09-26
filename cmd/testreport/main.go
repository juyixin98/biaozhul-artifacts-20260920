// Command testreport turns a `go test -json` event stream into a single
// structured JSON report. It reads events from stdin (or a file via -in)
// and writes an aggregated report to stdout (or -out). Exit code is 0 only
// when every package and test passed.
//
// Usage:
//
//	go test -json -race ./... | go run ./cmd/testreport -out results.json
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

// Event is one line of `go test -json`.
type Event struct {
	Time    time.Time `json:"Time"`
	Action  string    `json:"Action"`
	Package string    `json:"Package"`
	Test    string    `json:"Test"`
	Elapsed float64   `json:"Elapsed"`
	Output  string    `json:"Output"`
}

// TestResult aggregates one test.
type TestResult struct {
	Package    string `json:"package"`
	Name       string `json:"name"`
	Result     string `json:"result"` // pass | fail | skip
	ElapsedMS  int64  `json:"elapsed_ms"`
	OutputTail string `json:"output_tail,omitempty"`
}

// PackageResult aggregates one package.
type PackageResult struct {
	Name      string       `json:"name"`
	Result    string       `json:"result"` // pass | fail
	ElapsedMS int64        `json:"elapsed_ms"`
	Tests     []TestResult `json:"tests"`
}

// Totals summarizes the whole run.
type Totals struct {
	Packages       int `json:"packages"`
	PackagesFailed int `json:"packages_failed"`
	Tests          int `json:"tests"`
	Passed         int `json:"passed"`
	Failed         int `json:"failed"`
	Skipped        int `json:"skipped"`
}

// Report is the structured output.
type Report struct {
	GeneratedAt time.Time       `json:"generated_at"`
	Tool        string          `json:"tool"`
	OK          bool            `json:"ok"`
	DurationMS  int64           `json:"duration_ms"`
	Totals      Totals          `json:"totals"`
	Packages    []PackageResult `json:"packages"`
	Failures    []TestResult    `json:"failures"`
}

type agg struct {
	pkgOrder  []string
	pkgs      map[string]*PackageResult
	tests     map[string]*TestResult // key: pkg + "\x00" + test
	testOrder []string               // keys in observation order
	output    map[string]*strings.Builder
	start     time.Time
	end       time.Time
}

func newAgg() *agg {
	return &agg{
		pkgs:   map[string]*PackageResult{},
		tests:  map[string]*TestResult{},
		output: map[string]*strings.Builder{},
	}
}

func key(pkg, test string) string { return pkg + "\x00" + test }

func (a *agg) pkg(name string) *PackageResult {
	p, ok := a.pkgs[name]
	if !ok {
		p = &PackageResult{Name: name, Result: "pass"}
		a.pkgs[name] = p
		a.pkgOrder = append(a.pkgOrder, name)
	}
	return p
}

func (a *agg) consume(e Event) {
	if a.start.IsZero() || e.Time.Before(a.start) {
		a.start = e.Time
	}
	a.end = e.Time

	switch e.Action {
	case "start":
		a.pkg(e.Package)
	case "run":
		k := key(e.Package, e.Test)
		if _, seen := a.tests[k]; !seen {
			a.testOrder = append(a.testOrder, k)
		}
		a.tests[k] = &TestResult{Package: e.Package, Name: e.Test, Result: "run"}
		a.output[k] = &strings.Builder{}
	case "output":
		if e.Test != "" {
			if b, ok := a.output[key(e.Package, e.Test)]; ok {
				b.WriteString(e.Output)
			}
		}
	case "pass", "fail", "skip":
		if e.Test == "" {
			p := a.pkg(e.Package)
			p.Result = e.Action
			p.ElapsedMS = int64(e.Elapsed * 1000)
			return
		}
		t := a.tests[key(e.Package, e.Test)]
		if t == nil { // defensive: result without an observed run event
			t = &TestResult{Package: e.Package, Name: e.Test}
			a.tests[key(e.Package, e.Test)] = t
		}
		t.Result = e.Action
		t.ElapsedMS = int64(e.Elapsed * 1000)
	}
}

func (a *agg) build() Report {
	rep := Report{
		GeneratedAt: time.Now().UTC(),
		Tool:        "go test -json -> cmd/testreport",
		OK:          true,
		Packages:    []PackageResult{},
		Failures:    []TestResult{},
	}
	if !a.start.IsZero() {
		rep.DurationMS = a.end.Sub(a.start).Milliseconds()
	}

	for _, name := range a.pkgOrder {
		p := a.pkgs[name]
		rep.Totals.Packages++
		// A package with no test files reports action "skip"; that is not a
		// failure. Only an explicit "fail" fails the run.
		if p.Result == "fail" {
			rep.Totals.PackagesFailed++
			rep.OK = false
		}
		// Attach this package's tests in observation order.
		for _, k := range a.testOrder {
			if tr, ok := a.tests[k]; ok && tr.Package == name {
				p.Tests = append(p.Tests, *tr)
			}
		}
		rep.Packages = append(rep.Packages, *p)
	}

	for k, tr := range a.tests {
		rep.Totals.Tests++
		switch tr.Result {
		case "pass":
			rep.Totals.Passed++
		case "fail":
			rep.Totals.Failed++
			rep.OK = false
		case "skip":
			rep.Totals.Skipped++
		default:
			// A test left in "run" with no terminal event is a failure
			// (e.g. panic/timeout killed the process).
			tr.Result = "fail"
			rep.Totals.Failed++
			rep.OK = false
		}
		if tr.Result == "fail" {
			if b, ok := a.output[k]; ok {
				tail := b.String()
				if len(tail) > 2000 {
					tail = "..." + tail[len(tail)-2000:]
				}
				tr.OutputTail = tail
			}
			rep.Failures = append(rep.Failures, *tr)
		}
	}
	return rep
}

// ErrTestsFailed is returned when the aggregated test run had failures. It
// lets the CLI map the condition to exit code 1 without calling os.Exit
// inside the testable core.
var ErrTestsFailed = errors.New("one or more tests failed")

func run(in io.Reader, out io.Writer) error {
	a := newAgg()
	dec := json.NewDecoder(in)
	for dec.More() {
		var e Event
		if err := dec.Decode(&e); err != nil {
			return fmt.Errorf("decode test event: %w", err)
		}
		a.consume(e)
	}
	rep := a.build()
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	if err := enc.Encode(rep); err != nil {
		return err
	}
	if !rep.OK {
		return ErrTestsFailed
	}
	return nil
}

func main() {
	if err := mainErr(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		if errors.Is(err, ErrTestsFailed) {
			os.Exit(1) // tests failed
		}
		os.Exit(2) // usage / I/O / decode error
	}
}

// mainErr wires flags and files and runs the aggregation. A failed test run
// surfaces as a reportFail error carrying exit code 1; I/O/flag problems
// carry exit code 2.
func mainErr(args []string, stdin io.Reader, stdout io.Writer) error {
	fs := flag.NewFlagSet("testreport", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	inPath := fs.String("in", "", "go test -json input file (default stdin)")
	outPath := fs.String("out", "", "report output file (default stdout)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	in := stdin
	if *inPath != "" {
		f, err := os.Open(*inPath)
		if err != nil {
			return err
		}
		defer func() { _ = f.Close() }()
		in = f
	}
	out := stdout
	if *outPath != "" {
		f, err := os.Create(*outPath)
		if err != nil {
			return err
		}
		defer func() { _ = f.Close() }()
		out = f
	}
	return run(in, out)
}
