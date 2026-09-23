// Command simulator pushes synthetic, interleaved log lines at a running
// logpipe server, waits for timeout flushes, queries the assembled
// entries and verifies them. It exits non-zero if any expectation fails.
//
// Scenarios:
//
//	alpha / beta  — two sources interleaved line by line, each with
//	                multiple stack-trace entries; proves no cross-source
//	                mixing and per-source buffering.
//	gamma         — continuation lines without a start line (orphan).
//	delta         — a single line longer than the entry byte budget.
//	epsilon       — pid change mid-entry (process restart).
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"logpipe/internal/merger"
)

type lineDTO struct {
	Source string `json:"source"`
	PID    *int   `json:"pid,omitempty"`
	Text   string `json:"text"`
}

func pid(v int) *int { return &v }

type check struct {
	name     string
	source   string
	marker   string
	complete bool
	reason   string
	lines    int
	validate func(e merger.Entry) string
}

func main() {
	base := flag.String("url", "http://127.0.0.1:8080", "server base URL")
	wait := flag.Duration("wait", 3*time.Second, "time to wait for timeout flushes before querying")
	maxBytes := flag.Int("max-bytes", 4096, "server --max-bytes value, used for size assertions")
	flag.Parse()

	client := &http.Client{Timeout: 10 * time.Second}
	ts := time.Now().Format(time.RFC3339)
	head := func(body string) string { return ts + " " + body }

	// --- 1. Interleaved alpha/beta stacks ---------------------------
	alpha1 := pid(1001)
	beta1 := pid(2002)
	interleaved := []lineDTO{
		{Source: "alpha", PID: alpha1, Text: head("ERROR ALPHA-STACK-A boom in alpha")},
		{Source: "beta", PID: beta1, Text: head("ERROR BETA-STACK-D boom in beta")},
		{Source: "alpha", PID: alpha1, Text: "    [alpha] at alpha.fnA (alpha_a.go:10)"},
		{Source: "beta", PID: beta1, Text: "    [beta] at beta.fnD (beta_d.go:11)"},
		{Source: "alpha", PID: alpha1, Text: "    [alpha] at alpha.fnA2 (alpha_a.go:20)"},
		{Source: "beta", PID: beta1, Text: head("ERROR BETA-STACK-E second beta failure")},
		{Source: "alpha", PID: alpha1, Text: head("ERROR ALPHA-STACK-B middle alpha failure")},
		{Source: "beta", PID: beta1, Text: "    [beta] at beta.fnE1 (beta_e.go:1)"},
		{Source: "alpha", PID: alpha1, Text: "    [alpha] at alpha.fnB (alpha_b.go:30)"},
		{Source: "beta", PID: beta1, Text: "    [beta] at beta.fnE2 (beta_e.go:2)"},
		{Source: "alpha", PID: alpha1, Text: head("ERROR ALPHA-STACK-C trailing alpha failure")},
		{Source: "beta", PID: beta1, Text: "    [beta] at beta.fnE3 (beta_e.go:3)"},
		{Source: "alpha", PID: alpha1, Text: "    [alpha] at alpha.fnC (alpha_c.go:40)"},
	}
	post(client, *base+"/ingest", interleaved)

	// --- 2. gamma: orphan line without a start line -----------------
	gamma := pid(3003)
	post(client, *base+"/ingest", []lineDTO{
		{Source: "gamma", PID: gamma, Text: "    [gamma] GAMMA-ORPHAN stray frame with no header"},
	})
	post(client, *base+"/ingest", []lineDTO{
		{Source: "gamma", PID: gamma, Text: head("ERROR GAMMA-STACK-G gamma then fails properly")},
		{Source: "gamma", PID: gamma, Text: "    [gamma] at gamma.fnG (gamma_g.go:7)"},
	})

	// --- 3. delta: one oversized start line -------------------------
	delta := pid(4004)
	huge := head("FATAL DELTA-OVERFLOW ") + strings.Repeat("X", *maxBytes)
	post(client, *base+"/ingest", []lineDTO{
		{Source: "delta", PID: delta, Text: huge},
	})
	// Any continuation arriving after the budget is hit must be dropped,
	// not merged into another source.
	post(client, *base+"/ingest", []lineDTO{
		{Source: "delta", PID: delta, Text: "    [delta] DELTA-EXTRA frame after overflow"},
	})

	// --- 4. epsilon: process restart (pid change) -------------------
	post(client, *base+"/ingest", []lineDTO{
		{Source: "epsilon", PID: pid(5005), Text: head("WARN EPSILON-OLD pre-restart warning")},
		{Source: "epsilon", PID: pid(5005), Text: "    [epsilon] at epsilon.old (epsilon_old.go:1)"},
	})
	// New pid, new process: start line arrives while old entry pending.
	post(client, *base+"/ingest", []lineDTO{
		{Source: "epsilon", PID: pid(5006), Text: head("INFO EPSILON-NEW post-restart start")},
	})
	post(client, *base+"/ingest", []lineDTO{
		{Source: "epsilon", PID: pid(5006), Text: "    [epsilon] at epsilon.new (epsilon_new.go:2)"},
	})

	fmt.Printf("simulator: all lines posted; waiting %s for timeout flushes...\n", wait)
	time.Sleep(*wait)

	entries := query(client, *base+"/entries")
	fmt.Printf("simulator: got %d assembled entries\n\n", len(entries))

	markers := []string{
		"ALPHA-STACK-A", "ALPHA-STACK-B", "ALPHA-STACK-C",
		"BETA-STACK-D", "BETA-STACK-E",
		"GAMMA-ORPHAN", "GAMMA-STACK-G",
		"DELTA-OVERFLOW", "EPSILON-OLD", "EPSILON-NEW",
	}
	byMarker := map[string]merger.Entry{}
	for _, e := range entries {
		for _, mk := range markers {
			if strings.Contains(e.Text, mk) {
				if _, dup := byMarker[mk]; dup {
					fatal("marker %q appears in more than one entry", mk)
				}
				byMarker[mk] = e
			}
		}
	}

	checks := []check{
		{name: "alpha stack A (interleaved, closed by B)", source: "alpha", marker: "ALPHA-STACK-A", complete: true, reason: merger.ReasonNextStart, lines: 3},
		{name: "alpha stack B (interleaved, closed by C)", source: "alpha", marker: "ALPHA-STACK-B", complete: true, reason: merger.ReasonNextStart, lines: 2},
		{name: "alpha stack C (interleaved, timeout tail)", source: "alpha", marker: "ALPHA-STACK-C", complete: true, reason: merger.ReasonTimeout, lines: 2},
		{name: "beta stack D (interleaved, closed by E)", source: "beta", marker: "BETA-STACK-D", complete: true, reason: merger.ReasonNextStart, lines: 2},
		{name: "beta stack E (interleaved, timeout tail)", source: "beta", marker: "BETA-STACK-E", complete: true, reason: merger.ReasonTimeout, lines: 4},
		{name: "gamma orphan (no start line)", source: "gamma", marker: "GAMMA-ORPHAN", complete: false, reason: merger.ReasonOrphan, lines: 1},
		{name: "gamma stack G (timeout)", source: "gamma", marker: "GAMMA-STACK-G", complete: true, reason: merger.ReasonTimeout, lines: 2},
		{name: "delta overflow (single oversized line)", source: "delta", marker: "DELTA-OVERFLOW", complete: false, reason: merger.ReasonBytesLimit, lines: 1,
			validate: func(e merger.Entry) string {
				if !e.Truncated {
					return "truncated flag not set"
				}
				if e.Bytes > *maxBytes {
					return fmt.Sprintf("bytes %d exceeds budget %d", e.Bytes, *maxBytes)
				}
				if e.DroppedLines < 1 {
					return fmt.Sprintf("dropped_lines = %d, want >= 1", e.DroppedLines)
				}
				if strings.Contains(e.Text, "DELTA-EXTRA") {
					return "post-overflow continuation leaked into the entry text"
				}
				return ""
			}},
		{name: "epsilon old entry (process restart)", source: "epsilon", marker: "EPSILON-OLD", complete: false, reason: merger.ReasonRestart, lines: 2},
		{name: "epsilon new entry (post-restart, timeout)", source: "epsilon", marker: "EPSILON-NEW", complete: true, reason: merger.ReasonTimeout, lines: 2},
	}

	failed := 0
	for _, c := range checks {
		e, ok := byMarker[c.marker]
		if !ok {
			fail(c.name, "no entry contains marker")
			failed++
			continue
		}
		var problems []string
		if e.Source != c.source {
			problems = append(problems, fmt.Sprintf("source = %q, want %q", e.Source, c.source))
		}
		if e.Complete != c.complete {
			problems = append(problems, fmt.Sprintf("complete = %v, want %v", e.Complete, c.complete))
		}
		if e.Reason != c.reason {
			problems = append(problems, fmt.Sprintf("reason = %q, want %q", e.Reason, c.reason))
		}
		if e.LineCount != c.lines {
			problems = append(problems, fmt.Sprintf("line_count = %d, want %d", e.LineCount, c.lines))
		}
		// Cross-source isolation: no other marker may appear.
		for _, other := range markers {
			if other != c.marker && strings.Contains(e.Text, other) {
				problems = append(problems, fmt.Sprintf("foreign marker %q found in entry text", other))
			}
		}
		if c.validate != nil {
			if msg := c.validate(e); msg != "" {
				problems = append(problems, msg)
			}
		}
		if len(problems) == 0 {
			fmt.Printf("PASS  %-45s id=%d lines=%d bytes=%d reason=%s\n",
				c.name, e.ID, e.LineCount, e.Bytes, e.Reason)
		} else {
			failed++
			for _, p := range problems {
				fail(c.name, p)
			}
		}
	}

	fmt.Println()
	if failed == 0 {
		fmt.Printf("ALL CHECKS PASSED: %d entries, %d scenarios, no cross-source mixing\n", len(entries), len(checks))
		return
	}
	fatal("%d scenario(s) failed", failed)
}

func post(client *http.Client, url string, lines []lineDTO) {
	body, err := json.Marshal(lines)
	if err != nil {
		fatal("marshal: %v", err)
	}
	resp, err := client.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		fatal("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		data, _ := io.ReadAll(resp.Body)
		fatal("POST %s -> %s: %s", url, resp.Status, strings.TrimSpace(string(data)))
	}
}

func query(client *http.Client, url string) []merger.Entry {
	resp, err := client.Get(url)
	if err != nil {
		fatal("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(resp.Body)
		fatal("GET %s -> %s: %s", url, resp.Status, strings.TrimSpace(string(data)))
	}
	var out struct {
		Entries []merger.Entry `json:"entries"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		fatal("decode entries: %v", err)
	}
	return out.Entries
}

func fail(name, format string, args ...any) {
	fmt.Fprintf(os.Stderr, "FAIL  %-45s %s\n", name, fmt.Sprintf(format, args...))
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "simulator: "+format+"\n", args...)
	os.Exit(1)
}
