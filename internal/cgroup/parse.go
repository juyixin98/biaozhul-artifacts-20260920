// Package cgroup parses a strictly-defined subset of cgroup v2 stat files.
//
// Supported files and formats (anything else is a parse error):
//
//	cpu.stat        lines of "<key> <uint64>", known keys:
//	                usage_usec, user_usec, system_usec, nr_periods,
//	                nr_throttled, throttled_usec
//	memory.current  a single unsigned integer (bytes)
//	memory.max      a single unsigned integer (bytes) or the literal "max"
//	memory.events   lines of "<key> <uint64>", known keys:
//	                low, high, max, oom, oom_kill, oom_group_kill
//	cpu.pressure / memory.pressure (PSI):
//	                two lines, "some" and "full", each:
//	                <some|full> avg10=<float> avg60=<float> avg300=<float> total=<uint64>
//
// Unknown keys inside known files are ignored but reported as warnings so the
// caller can surface them instead of silently dropping data.
package cgroup

import (
	"fmt"
	"strconv"
	"strings"
)

// CPUStat is the parsed subset of cgroup v2 cpu.stat.
// All counters are cumulative since cgroup creation, in microseconds.
type CPUStat struct {
	UsageUsec     uint64 `json:"usage_usec"`
	UserUsec      uint64 `json:"user_usec"`
	SystemUsec    uint64 `json:"system_usec"`
	NrPeriods     uint64 `json:"nr_periods"`
	NrThrottled   uint64 `json:"nr_throttled"`
	ThrottledUsec uint64 `json:"throttled_usec"`
}

// MemoryEvents is the parsed subset of cgroup v2 memory.events.
// All fields are cumulative event counters.
type MemoryEvents struct {
	Low          uint64 `json:"low"`
	High         uint64 `json:"high"`
	Max          uint64 `json:"max"`
	Oom          uint64 `json:"oom"`
	OomKill      uint64 `json:"oom_kill"`
	OomGroupKill uint64 `json:"oom_group_kill"`
}

// PressureLine is one PSI line ("some" or "full").
// Avg* are percentages over 10/60/300 second windows; Total is cumulative
// stall time in microseconds.
type PressureLine struct {
	Avg10  float64 `json:"avg10"`
	Avg60  float64 `json:"avg60"`
	Avg300 float64 `json:"avg300"`
	Total  uint64  `json:"total"`
}

// Pressure is a parsed PSI file (cpu.pressure or memory.pressure).
type Pressure struct {
	Some PressureLine `json:"some"`
	Full PressureLine `json:"full"`
}

// parseKVUint64 parses "<key> <uint64>" lines for the given known keys.
// Unknown keys produce warnings; malformed lines produce an error.
func parseKVUint64(content string, known map[string]*uint64, fileName string) ([]string, error) {
	var warnings []string
	lines := strings.Split(strings.TrimRight(content, "\n"), "\n")
	for i, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) != 2 {
			return nil, fmt.Errorf("%s:%d: expected \"<key> <value>\", got %q", fileName, i+1, line)
		}
		dst, ok := known[fields[0]]
		if !ok {
			warnings = append(warnings, fmt.Sprintf("%s:%d: unknown key %q ignored", fileName, i+1, fields[0]))
			continue
		}
		v, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return nil, fmt.Errorf("%s:%d: key %q: %v", fileName, i+1, fields[0], err)
		}
		*dst = v
	}
	return warnings, nil
}

// ParseCPUStat parses a cpu.stat file.
func ParseCPUStat(content string) (CPUStat, []string, error) {
	var s CPUStat
	w, err := parseKVUint64(content, map[string]*uint64{
		"usage_usec":     &s.UsageUsec,
		"user_usec":      &s.UserUsec,
		"system_usec":    &s.SystemUsec,
		"nr_periods":     &s.NrPeriods,
		"nr_throttled":   &s.NrThrottled,
		"throttled_usec": &s.ThrottledUsec,
	}, "cpu.stat")
	return s, w, err
}

// ParseMemoryEvents parses a memory.events file.
func ParseMemoryEvents(content string) (MemoryEvents, []string, error) {
	var e MemoryEvents
	w, err := parseKVUint64(content, map[string]*uint64{
		"low":            &e.Low,
		"high":           &e.High,
		"max":            &e.Max,
		"oom":            &e.Oom,
		"oom_kill":       &e.OomKill,
		"oom_group_kill": &e.OomGroupKill,
	}, "memory.events")
	return e, w, err
}

// ParseUint64File parses a file containing a single unsigned integer
// (memory.current). Returns the value in bytes.
func ParseUint64File(content, fileName string) (uint64, error) {
	s := strings.TrimSpace(content)
	v, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s: expected a single unsigned integer, got %q", fileName, s)
	}
	return v, nil
}

// ParseMemoryMax parses memory.max: a byte count or the literal "max"
// (no limit). unlimited is true when the value is "max".
func ParseMemoryMax(content string) (value uint64, unlimited bool, err error) {
	s := strings.TrimSpace(content)
	if s == "max" {
		return 0, true, nil
	}
	v, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return 0, false, fmt.Errorf("memory.max: expected unsigned integer or \"max\", got %q", s)
	}
	return v, false, nil
}

// ParsePressure parses a PSI pressure file (cpu.pressure / memory.pressure).
func ParsePressure(content, fileName string) (Pressure, []string, error) {
	var p Pressure
	var warnings []string
	lines := strings.Split(strings.TrimRight(content, "\n"), "\n")
	for i, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) != 5 {
			return p, warnings, fmt.Errorf("%s:%d: expected 5 fields, got %q", fileName, i+1, line)
		}
		var dst *PressureLine
		switch fields[0] {
		case "some":
			dst = &p.Some
		case "full":
			dst = &p.Full
		default:
			return p, warnings, fmt.Errorf("%s:%d: expected \"some\" or \"full\", got %q", fileName, i+1, fields[0])
		}
		for _, kv := range fields[1:] {
			k, v, ok := strings.Cut(kv, "=")
			if !ok {
				return p, warnings, fmt.Errorf("%s:%d: expected key=value, got %q", fileName, i+1, kv)
			}
			switch k {
			case "avg10", "avg60", "avg300":
				f, err := strconv.ParseFloat(v, 64)
				if err != nil {
					return p, warnings, fmt.Errorf("%s:%d: %s: %v", fileName, i+1, k, err)
				}
				switch k {
				case "avg10":
					dst.Avg10 = f
				case "avg60":
					dst.Avg60 = f
				case "avg300":
					dst.Avg300 = f
				}
			case "total":
				t, err := strconv.ParseUint(v, 10, 64)
				if err != nil {
					return p, warnings, fmt.Errorf("%s:%d: total: %v", fileName, i+1, err)
				}
				dst.Total = t
			default:
				return p, warnings, fmt.Errorf("%s:%d: unknown PSI key %q", fileName, i+1, k)
			}
		}
	}
	return p, warnings, nil
}
