// Package cgfmt parses the strict, explicitly-scoped subset of cgroup v2
// pseudo-files that this project samples offline.
//
// Every parser is deliberately strict: cgroup files are machine generated and
// their layout is part of the input contract. A malformed file is a hard error
// surfaced to the caller; parsers never silently skip fields or guess units.
//
// Accepted formats (exact, one key per line, single spaces):
//
//	cpu.stat           "key uint64" lines
//	                   keys: usage_usec*, user_usec, system_usec,
//	                         nr_periods, nr_throttled, throttled_usec
//	memory.events      "key uint64" lines
//	                   keys: low, high, max, oom, oom_kill
//	memory.current     single line, one uint64
//	memory.max         single line, one uint64 OR the literal "max"
//	memory.oom.group   single line, "0" or "1"
//	pids.current       single line, one uint64
//	pids.events        "key uint64" lines; keys: max
//	cpu.pressure /
//	memory.pressure    PSI v1 (exact kernel spelling, '=' attached):
//	                   "some avg10=1.23 avg60=4.56 avg300=7.89 total=12345"
//	                   [ "full avg10=... avg60=... avg300=... total=..." ]
//	                   cpu.pressure never carries a "full" line (the kernel
//	                   excludes it); that is validated at load time.
//	instance.id        single line, 1-64 chars from [A-Za-z0-9._-]
//
// * usage_usec is the only required cpu.stat counter; the other five are
//   optional and simply absent from older kernels.
//
// Timestamps are not read from the files: sample instant is the directory name
// (see pkg/cgsample), so clock skew inside a fixture cannot leak into rates.
package cgfmt

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
)

// CPUStat holds the monotonic nanosecond-resolution counters from cpu.stat.
// Usage is required; the rest are nil when the kernel did not emit the key.
type CPUStat struct {
	UsageUsec     uint64
	UserUsec      *uint64
	SystemUsec    *uint64
	NrPeriods     *uint64
	NrThrottled   *uint64
	ThrottledUsec *uint64
}

// MemoryEvents holds the counters from memory.events. All five are required:
// the kernel always emits them and a missing one would hide an OOM.
type MemoryEvents struct {
	Low     uint64
	High    uint64
	Max     uint64
	OOM     uint64
	OOMKill uint64
}

// PSILine is one line ("some" or "full") of a PSI file.
type PSILine struct {
	Avg10  float64
	Avg60  float64
	Avg300 float64
	Total  uint64 // microseconds stalled since cgroup creation
}

// PSI is a parsed cpu.pressure / memory.pressure file.
type PSI struct {
	Some PSILine
	Full *PSILine // nil for cpu.pressure
}

var (
	errDuplicateKey = errors.New("duplicate key")
	errUnknownKey   = errors.New("unknown key")
	errMissingKey   = errors.New("missing required key")
)

func isBlank(line []byte) bool { return len(strings.TrimSpace(string(line))) == 0 }

// checkNoBlankReject: blank lines are only tolerated as a single trailing one.
func splitNonEmpty(data []byte) ([][]byte, error) {
	r := bufio.NewScanner(bytes.NewReader(data))
	r.Buffer(make([]byte, 1024*1024), 1024*1024)
	var out [][]byte
	for r.Scan() {
		line := r.Bytes()
		cp := append([]byte(nil), line...)
		out = append(out, cp)
	}
	if err := r.Err(); err != nil {
		return nil, err
	}
	// Tolerate exactly one trailing blank line (final newline).
	for len(out) > 0 && isBlank(out[len(out)-1]) {
		out = out[:len(out)-1]
	}
	for i, l := range out {
		if isBlank(l) {
			return nil, fmt.Errorf("blank line at line %d", i+1)
		}
	}
	return out, nil
}

// parseIntKV parses "key value" lines into a map, enforcing uniqueness and
// single-space separators (no tabs, no repeated spaces).
func parseIntKV(data []byte, allowed map[string]bool) (map[string]uint64, error) {
	lines, err := splitNonEmpty(data)
	if err != nil {
		return nil, err
	}
	if len(lines) == 0 {
		return nil, errors.New("empty file")
	}
	m := make(map[string]uint64, len(lines))
	for i, raw := range lines {
		line := string(raw)
		parts := strings.Split(line, " ")
		if len(parts) != 2 {
			return nil, fmt.Errorf("line %d: expected exactly 2 space-separated fields, got %d", i+1, len(parts))
		}
		key, val := parts[0], parts[1]
		if !allowed[key] {
			return nil, fmt.Errorf("line %d: %w %q", i+1, errUnknownKey, key)
		}
		if _, ok := m[key]; ok {
			return nil, fmt.Errorf("line %d: %w %q", i+1, errDuplicateKey, key)
		}
		n, err := parseU64(val)
		if err != nil {
			return nil, fmt.Errorf("line %d key %q: %w", i+1, key, err)
		}
		m[key] = n
	}
	return m, nil
}

func parseU64(s string) (uint64, error) {
	if s == "" {
		return 0, errors.New("empty integer")
	}
	n, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid unsigned integer %q: %v", s, err)
	}
	return n, nil
}

func parseSingleU64(data []byte) (uint64, error) {
	lines, err := splitNonEmpty(data)
	if err != nil {
		return 0, err
	}
	if len(lines) != 1 {
		return 0, fmt.Errorf("expected exactly 1 line, got %d", len(lines))
	}
	fields := strings.Split(string(lines[0]), " ")
	if len(fields) != 1 {
		return 0, fmt.Errorf("expected a single integer, got %d fields", len(fields))
	}
	return parseU64(fields[0])
}

// ParseCPUStat parses cpu.stat.
func ParseCPUStat(data []byte) (CPUStat, error) {
	allowed := map[string]bool{
		"usage_usec": true, "user_usec": true, "system_usec": true,
		"nr_periods": true, "nr_throttled": true, "throttled_usec": true,
	}
	m, err := parseIntKV(data, allowed)
	if err != nil {
		return CPUStat{}, err
	}
	var out CPUStat
	var ok bool
	if out.UsageUsec, ok = m["usage_usec"]; !ok {
		return out, fmt.Errorf("%w usage_usec", errMissingKey)
	}
	ptr := func(k string) *uint64 {
		if v, present := m[k]; present {
			return &v
		}
		return nil
	}
	out.UserUsec = ptr("user_usec")
	out.SystemUsec = ptr("system_usec")
	out.NrPeriods = ptr("nr_periods")
	out.NrThrottled = ptr("nr_throttled")
	out.ThrottledUsec = ptr("throttled_usec")
	return out, nil
}

// ParseMemoryEvents parses memory.events with all five counters mandatory.
func ParseMemoryEvents(data []byte) (MemoryEvents, error) {
	allowed := map[string]bool{
		"low": true, "high": true, "max": true, "oom": true, "oom_kill": true,
	}
	m, err := parseIntKV(data, allowed)
	if err != nil {
		return MemoryEvents{}, err
	}
	for _, k := range []string{"low", "high", "max", "oom", "oom_kill"} {
		if _, ok := m[k]; !ok {
			return MemoryEvents{}, fmt.Errorf("%w %s", errMissingKey, k)
		}
	}
	return MemoryEvents{Low: m["low"], High: m["high"], Max: m["max"], OOM: m["oom"], OOMKill: m["oom_kill"]}, nil
}

// ParseMemoryCurrent parses memory.current (bytes).
func ParseMemoryCurrent(data []byte) (uint64, error) { return parseSingleU64(data) }

// ParsePIDsCurrent parses pids.current (count).
func ParsePIDsCurrent(data []byte) (uint64, error) { return parseSingleU64(data) }

// ParseMemoryMax parses memory.max: a byte count or the literal "max"
// (unlimited), which is reported as ok=false.
func ParseMemoryMax(data []byte) (bytes uint64, ok bool, err error) {
	lines, perr := splitNonEmpty(data)
	if perr != nil {
		return 0, false, perr
	}
	if len(lines) != 1 {
		return 0, false, fmt.Errorf("expected exactly 1 line, got %d", len(lines))
	}
	s := string(lines[0])
	if strings.Contains(s, " ") {
		return 0, false, errors.New("expected a single token")
	}
	if s == "max" {
		return 0, false, nil
	}
	n, perr := parseU64(s)
	if perr != nil {
		return 0, false, perr
	}
	return n, true, nil
}

// ParseMemoryOOMGroup parses memory.oom.group ("0"/"1").
func ParseMemoryOOMGroup(data []byte) (bool, error) {
	n, err := parseSingleU64(data)
	if err != nil {
		return false, err
	}
	if n > 1 {
		return false, fmt.Errorf("memory.oom.group must be 0 or 1, got %d", n)
	}
	return n == 1, nil
}

// ParsePIDsEvents parses pids.events (only the "max" counter exists).
func ParsePIDsEvents(data []byte) (max uint64, err error) {
	m, err := parseIntKV(data, map[string]bool{"max": true})
	if err != nil {
		return 0, err
	}
	if _, ok := m["max"]; !ok {
		return 0, fmt.Errorf("%w max", errMissingKey)
	}
	return m["max"], nil
}

// ParsePSI parses a PSI v1 file. requireFull selects memory.pressure (true)
// vs cpu.pressure (false — a "full" line is impossible there and rejected).
func ParsePSI(data []byte, kind string) (PSI, error) {
	if kind != "cpu" && kind != "memory" {
		return PSI{}, fmt.Errorf("internal: unknown PSI kind %q", kind)
	}
	lines, err := splitNonEmpty(data)
	if err != nil {
		return PSI{}, err
	}
	if len(lines) < 1 || len(lines) > 2 {
		return PSI{}, fmt.Errorf("expected 1 or 2 PSI lines, got %d", len(lines))
	}
	var out PSI
	seen := map[string]bool{}
	for i, raw := range lines {
		line, perr := parsePSILine(string(raw), i+1)
		if perr != nil {
			return PSI{}, perr
		}
		if seen[line.label] {
			return PSI{}, fmt.Errorf("line %d: duplicate %q block", i+1, line.label)
		}
		seen[line.label] = true
		switch line.label {
		case "some":
			out.Some = line.PSILine
		case "full":
			if kind == "cpu" {
				return PSI{}, errors.New("cpu.pressure must not contain a \"full\" line")
			}
			f := line.PSILine
			out.Full = &f
		default:
			return PSI{}, fmt.Errorf("line %d: PSI block must be \"some\" or \"full\", got %q", i+1, line.label)
		}
	}
	if kind == "memory" && out.Full == nil {
		return PSI{}, errors.New("memory.pressure missing required \"full\" line")
	}
	return out, nil
}

type parsedPSILine struct {
	label string
	PSILine
}

func parsePSILine(line string, n int) (parsedPSILine, error) {
	// Real kernel format (single spaces, '=' attached, no spaces around '='):
	//   some avg10=1.23 avg60=4.56 avg300=7.89 total=12345
	fs := strings.Split(line, " ")
	if len(fs) != 5 {
		return parsedPSILine{}, fmt.Errorf("line %d: expected 5 space-separated fields, got %d (%q)", n, len(fs), line)
	}
	label := fs[0]
	if label != "some" && label != "full" {
		return parsedPSILine{}, fmt.Errorf("line %d: PSI block must be \"some\" or \"full\", got %q", n, label)
	}
	want := []struct {
		idx    int
		name   string
	}{{1, "avg10"}, {2, "avg60"}, {3, "avg300"}, {4, "total"}}
	vals := map[string]string{}
	for _, w := range want {
		tok := fs[w.idx]
		prefix := w.name + "="
		if !strings.HasPrefix(tok, prefix) {
			return parsedPSILine{}, fmt.Errorf("line %d: expected field %q (e.g. %s0.00), got %q", n, w.name, prefix, tok)
		}
		vals[w.name] = tok[len(prefix):]
	}
	parseF := func(k string) (float64, error) {
		f, err := strconv.ParseFloat(vals[k], 64)
		if err != nil {
			return 0, fmt.Errorf("invalid float for %s: %v", k, err)
		}
		if math.IsNaN(f) || math.IsInf(f, 0) || f < 0 {
			return 0, fmt.Errorf("%s value out of range", k)
		}
		return f, nil
	}
	a10, err := parseF("avg10")
	if err != nil {
		return parsedPSILine{}, fmt.Errorf("line %d: %w", n, err)
	}
	a60, err := parseF("avg60")
	if err != nil {
		return parsedPSILine{}, fmt.Errorf("line %d: %w", n, err)
	}
	a300, err := parseF("avg300")
	if err != nil {
		return parsedPSILine{}, fmt.Errorf("line %d: %w", n, err)
	}
	total, err := parseU64(vals["total"])
	if err != nil {
		return parsedPSILine{}, fmt.Errorf("line %d: %w", n, err)
	}
	return parsedPSILine{label: label, PSILine: PSILine{Avg10: a10, Avg60: a60, Avg300: a300, Total: total}}, nil
}

// validInstanceID is the deliberately narrow instance.id alphabet.
func validInstanceID(s string) bool {
	if len(s) < 1 || len(s) > 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z':
		case c >= 'A' && c <= 'Z':
		case c >= '0' && c <= '9':
		case c == '.' || c == '_' || c == '-':
		default:
			return false
		}
	}
	return true
}

// ParseInstanceID parses the optional instance.id sentinel.
func ParseInstanceID(data []byte) (string, error) {
	lines, err := splitNonEmpty(data)
	if err != nil {
		return "", err
	}
	if len(lines) != 1 {
		return "", fmt.Errorf("instance.id: expected exactly 1 line, got %d", len(lines))
	}
	s := string(lines[0])
	if !validInstanceID(s) {
		return "", fmt.Errorf("instance.id: must be 1-64 chars of [A-Za-z0-9._-], got %q", s)
	}
	return s, nil
}

// Ensure io is referenced (kept for reader-based helpers used by the loader).
var _ io.Reader = (*bytes.Reader)(nil)
