package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func emit(w *bytes.Buffer, e Event) {
	b, _ := json.Marshal(e)
	w.Write(b)
	w.WriteByte('\n')
}

func base(ts string) time.Time {
	t, _ := time.Parse(time.RFC3339, ts)
	return t
}

func TestRun_PassingRunIsOK(t *testing.T) {
	var in bytes.Buffer
	pkg := "example.com/p"
	emit(&in, Event{Time: base("2026-01-01T00:00:00Z"), Action: "start", Package: pkg})
	emit(&in, Event{Time: base("2026-01-01T00:00:00Z"), Action: "run", Package: pkg, Test: "TestA"})
	emit(&in, Event{Time: base("2026-01-01T00:00:00Z"), Action: "output", Package: pkg, Test: "TestA", Output: "ok\n"})
	emit(&in, Event{Time: base("2026-01-01T00:00:01Z"), Action: "pass", Package: pkg, Test: "TestA", Elapsed: 1})
	emit(&in, Event{Time: base("2026-01-01T00:00:01Z"), Action: "pass", Package: pkg, Elapsed: 1})

	// run() would call os.Exit on failure; for a passing run it returns nil.
	var out bytes.Buffer
	if err := run(&in, &out); err != nil {
		t.Fatalf("run: %v", err)
	}
	var rep Report
	if err := json.Unmarshal(out.Bytes(), &rep); err != nil {
		t.Fatalf("decode report: %v\n%s", err, out.String())
	}
	if !rep.OK || rep.Totals.Tests != 1 || rep.Totals.Passed != 1 {
		t.Fatalf("unexpected report: %+v", rep.Totals)
	}
	if len(rep.Packages) != 1 || len(rep.Packages[0].Tests) != 1 {
		t.Fatalf("package/test nesting wrong: %+v", rep.Packages)
	}
	if rep.DurationMS != 1000 {
		t.Fatalf("duration = %d, want 1000", rep.DurationMS)
	}
}

func TestAgg_FailureAndSkipAndNoTestFiles(t *testing.T) {
	a := newAgg()
	pkgNoTests := "example.com/notests"
	emit2(a, Event{Action: "start", Package: pkgNoTests})
	emit2(a, Event{Action: "skip", Package: pkgNoTests})

	pkg := "example.com/p"
	emit2(a, Event{Action: "start", Package: pkg})
	emit2(a, Event{Action: "run", Package: pkg, Test: "TestGood"})
	emit2(a, Event{Action: "pass", Package: pkg, Test: "TestGood", Elapsed: 0.1})
	emit2(a, Event{Action: "run", Package: pkg, Test: "TestBad"})
	emit2(a, Event{Action: "output", Package: pkg, Test: "TestBad", Output: "boom\n"})
	emit2(a, Event{Action: "fail", Package: pkg, Test: "TestBad", Elapsed: 0.2})
	emit2(a, Event{Action: "run", Package: pkg, Test: "TestSkip"})
	emit2(a, Event{Action: "skip", Package: pkg, Test: "TestSkip", Elapsed: 0})
	emit2(a, Event{Action: "fail", Package: pkg, Elapsed: 0.5})

	rep := a.build()
	if rep.OK {
		t.Fatal("expected not OK")
	}
	if rep.Totals.Failed != 1 || rep.Totals.Passed != 1 || rep.Totals.Skipped != 1 {
		t.Fatalf("totals wrong: %+v", rep.Totals)
	}
	if rep.Totals.Packages != 2 || rep.Totals.PackagesFailed != 1 {
		t.Fatalf("package totals wrong: %+v", rep.Totals)
	}
	if len(rep.Failures) != 1 || rep.Failures[0].Name != "TestBad" {
		t.Fatalf("failures wrong: %+v", rep.Failures)
	}
	if !strings.Contains(rep.Failures[0].OutputTail, "boom") {
		t.Fatal("failure output tail not captured")
	}
}

func TestRun_BadJSONErrors(t *testing.T) {
	if err := run(bytes.NewBufferString("{not json"), &bytes.Buffer{}); err == nil {
		t.Fatal("expected decode error")
	}
}

func TestRun_UnfinishedTestCountsAsFailure(t *testing.T) {
	a := newAgg()
	pkg := "example.com/p"
	emit2(a, Event{Action: "start", Package: pkg})
	emit2(a, Event{Action: "run", Package: pkg, Test: "TestHung"})
	// No terminal event (process killed by timeout).
	rep := a.build()
	if rep.OK || rep.Totals.Failed != 1 {
		t.Fatalf("hung test must be counted as failure: %+v", rep.Totals)
	}
}

func TestRun_FailingRunReturnsSentinel(t *testing.T) {
	var in bytes.Buffer
	pkg := "example.com/p"
	emit(&in, Event{Action: "start", Package: pkg})
	emit(&in, Event{Action: "run", Package: pkg, Test: "TestBad"})
	emit(&in, Event{Action: "fail", Package: pkg, Test: "TestBad", Elapsed: 1})
	emit(&in, Event{Action: "fail", Package: pkg, Elapsed: 1})
	var out bytes.Buffer
	err := run(&in, &out)
	if !errors.Is(err, ErrTestsFailed) {
		t.Fatalf("err = %v, want ErrTestsFailed", err)
	}
}

func TestMainErr_FilePathsRoundTrip(t *testing.T) {
	dir := t.TempDir()
	inFile := filepath.Join(dir, "events.jsonl")
	outFile := filepath.Join(dir, "report.json")
	pkg := "example.com/p"
	var buf bytes.Buffer
	emit(&buf, Event{Action: "start", Package: pkg})
	emit(&buf, Event{Action: "run", Package: pkg, Test: "TestA"})
	emit(&buf, Event{Action: "pass", Package: pkg, Test: "TestA", Elapsed: 1})
	emit(&buf, Event{Action: "pass", Package: pkg, Elapsed: 1})
	if err := os.WriteFile(inFile, buf.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := mainErr([]string{"-in", inFile, "-out", outFile}, nil, nil); err != nil {
		t.Fatalf("mainErr: %v", err)
	}
	data, err := os.ReadFile(outFile)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `"ok": true`) {
		t.Fatalf("report file contents wrong: %s", data)
	}
}

func TestMainErr_BadFlagAndMissingFile(t *testing.T) {
	if err := mainErr([]string{"-no-such-flag"}, nil, nil); err == nil {
		t.Fatal("expected flag parse error")
	}
	if err := mainErr([]string{"-in", "/nonexistent/path/x.jsonl"}, nil, nil); err == nil {
		t.Fatal("expected missing input file error")
	}
	dir := t.TempDir()
	if err := mainErr([]string{"-out", filepath.Join(dir, "no", "dir", "r.json")}, nil, nil); err == nil {
		t.Fatal("expected output path error")
	}
}

func emit2(a *agg, e Event) { a.consume(e) }
