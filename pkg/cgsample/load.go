// Package cgsample loads offline cgroup v2 samples from a fixture root:
//
//	<root>/<container>/<UTC-timestamp>/<cgroup-file>
//
// A timestamp directory is the sampling instant. Its name must be UTC RFC3339
// (e.g. 2026-09-24T10:00:00Z or with a fractional second); no cgroup file
// carries a timestamp, so the directory name is the only trusted clock.
//
// Each sample reads as a whole unit and reports per-file parse errors instead
// of guessing. The SHA-256 of every read file is kept so API output can show
// exactly which bytes an event was derived from.
package cgsample

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"cresnap/pkg/cgfmt"
)

// File names that make up the input contract.
const (
	FileCPUStat       = "cpu.stat"
	FileCPUPressure   = "cpu.pressure"
	FileMemoryCurrent = "memory.current"
	FileMemoryEvents  = "memory.events"
	FileMemoryMax     = "memory.max"
	FileMemoryOOMGrp  = "memory.oom.group"
	FileMemPressure   = "memory.pressure"
	FilePIDsCurrent   = "pids.current"
	FilePIDsEvents    = "pids.events"
	FileInstanceID    = "instance.id"
)

// RequiredFiles are mandatory in every usable sample. Missing one invalidates
// the sample — an analyzer must not derive OOM/exit conclusions from a sample
// whose memory.events it never saw.
var RequiredFiles = []string{
	FileCPUStat, FileCPUPressure,
	FileMemoryCurrent, FileMemoryEvents, FileMemoryMax, FileMemPressure,
}

// OptionalFiles are read when present and noted, never required.
var OptionalFiles = []string{FileMemoryOOMGrp, FilePIDsCurrent, FilePIDsEvents, FileInstanceID}

// Parsed is the fully decoded content of one timestamp directory.
type Parsed struct {
	CPUStat        cgfmt.CPUStat
	CPUPressure    cgfmt.PSI
	MemoryCurrent  uint64
	MemoryEvents   cgfmt.MemoryEvents
	MemoryMaxBytes uint64
	MemoryLimited  bool // false when memory.max == "max"
	OOMGroup       *bool
	MemPressure    cgfmt.PSI
	PIDsCurrent    *uint64
	PIDsMaxEvents  *uint64
	InstanceID     *string
}

// FileHash binds a file name to the digest of the bytes actually parsed.
type FileHash struct {
	Name string `json:"name"`
	SHA256 string `json:"sha256"`
	Bytes  int64  `json:"bytes"`
}

// Sample is one directory: identity, decoded payload and provenance.
type Sample struct {
	Container string
	Time      time.Time
	DirName   string
	Path      string
	Parsed    Parsed
	Files     []FileHash // sorted by name; includes required + present optional
}

// LoadError is a per-container failure collected during a root scan.
type LoadError struct {
	Container string `json:"container"`
	DirName   string `json:"dir,omitempty"`
	Path      string `json:"path"`
	Op        string `json:"op"`
	Err       string `json:"error"`
}

func (e LoadError) Error() string {
	if e.DirName != "" {
		return fmt.Sprintf("%s: %s/%s: %s: %s", e.Op, e.Container, e.DirName, e.Path, e.Err)
	}
	return fmt.Sprintf("%s: %s: %s: %s", e.Op, e.Container, e.Path, e.Err)
}

// RootResult is everything found under a fixture root.
type RootResult struct {
	Root       string
	Containers map[string][]*Sample // container -> samples sorted by time
	Errors     []LoadError
}

const timestampLayout = time.RFC3339

// LoadRoot scans root, validates every file name and parses every sample.
// Samples with parse errors are excluded from Containers (an interval is only
// computed between two valid samples) but the error is always returned.
func LoadRoot(root string) (*RootResult, error) {
	out := &RootResult{Root: root, Containers: map[string][]*Sample{}}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, fmt.Errorf("read fixture root %q: %w", root, err)
	}
	for _, ce := range entries {
		if !ce.IsDir() {
			out.Errors = append(out.Errors, LoadError{
				Container: ce.Name(), Path: ce.Name(), Op: "scan",
				Err: "unexpected file at container level (expected directory)",
			})
			continue
		}
		container := ce.Name()
		cdir := filepath.Join(root, container)
		sdirs, err := os.ReadDir(cdir)
		if err != nil {
			out.Errors = append(out.Errors, LoadError{Container: container, Path: cdir, Op: "scan", Err: err.Error()})
			continue
		}
		var samples []*Sample
		for _, de := range sdirs {
			if !de.IsDir() {
				out.Errors = append(out.Errors, LoadError{
					Container: container, DirName: de.Name(), Path: de.Name(), Op: "scan",
					Err: "unexpected file at sample level (expected timestamp directory)",
				})
				continue
			}
			ts, terr := time.Parse(timestampLayout, de.Name())
			if terr != nil {
				out.Errors = append(out.Errors, LoadError{
					Container: container, DirName: de.Name(), Path: de.Name(), Op: "timestamp",
					Err: "directory name must be UTC RFC3339, e.g. 2026-09-24T10:00:00Z: " + terr.Error(),
				})
				continue
			}
			if ts.Location() != time.UTC {
				out.Errors = append(out.Errors, LoadError{
					Container: container, DirName: de.Name(), Path: de.Name(), Op: "timestamp",
					Err: "timestamp must be UTC (end with Z)",
				})
				continue
			}
			s, perr := loadSample(container, ts, de.Name(), filepath.Join(cdir, de.Name()))
			if perr != nil {
				out.Errors = append(out.Errors, *perr)
				continue
			}
			samples = append(samples, s)
		}
		sort.Slice(samples, func(i, j int) bool { return samples[i].Time.Before(samples[j].Time) })
		// Duplicate timestamps are ambiguous: reject the later one explicitly.
		for i := 1; i < len(samples); i++ {
			if samples[i].Time.Equal(samples[i-1].Time) {
				out.Errors = append(out.Errors, LoadError{
					Container: container, DirName: samples[i].DirName, Path: samples[i].Path,
					Op: "timestamp", Err: "duplicate sampling instant",
				})
				samples = append(samples[:i], samples[i+1:]...)
				i--
			}
		}
		out.Containers[container] = samples
	}
	return out, nil
}

func loadSample(container string, ts time.Time, dirName, dir string) (*Sample, *LoadError) {
	fail := func(op, name, msg string) *LoadError {
		return &LoadError{Container: container, DirName: dirName, Path: name, Op: op, Err: msg}
	}
	names, err := readFileNames(dir)
	if err != nil {
		return nil, fail("readdir", dirName, err.Error())
	}
	known := map[string]bool{}
	for _, n := range append(append([]string{}, RequiredFiles...), OptionalFiles...) {
		known[n] = true
	}
	for _, n := range names {
		if !known[n] {
			return nil, fail("filename", n, "file is not part of the recognized cgroup v2 fixture format")
		}
	}
	for _, req := range RequiredFiles {
		if !contains(names, req) {
			return nil, fail("missing", req, "required file is absent; sample cannot be analyzed")
		}
	}
	s := &Sample{Container: container, Time: ts, DirName: dirName, Path: dir}
	read := func(name string) ([]byte, bool) {
		b, rerr := os.ReadFile(filepath.Join(dir, name))
		if rerr != nil {
			return nil, false
		}
		sum := sha256.Sum256(b)
		s.Files = append(s.Files, FileHash{Name: name, SHA256: hex.EncodeToString(sum[:]), Bytes: int64(len(b))})
		return b, true
	}
	// readParse reads and parses a file; on any failure it returns a LoadError.
	rp := func(name string, parse func([]byte) error) *LoadError {
		b, ok := read(name)
		if !ok {
			return fail("read", name, "cannot read file")
		}
		if perr := parse(b); perr != nil {
			return fail("parse", name, perr.Error())
		}
		return nil
	}

	if le := rp(FileCPUStat, func(b []byte) (e error) {
		s.Parsed.CPUStat, e = cgfmt.ParseCPUStat(b)
		return e
	}); le != nil {
		return nil, le
	}
	if le := rp(FileCPUPressure, func(b []byte) (e error) {
		s.Parsed.CPUPressure, e = cgfmt.ParsePSI(b, "cpu")
		return e
	}); le != nil {
		return nil, le
	}
	if le := rp(FileMemoryCurrent, func(b []byte) (e error) {
		s.Parsed.MemoryCurrent, e = cgfmt.ParseMemoryCurrent(b)
		return e
	}); le != nil {
		return nil, le
	}
	if le := rp(FileMemoryEvents, func(b []byte) (e error) {
		s.Parsed.MemoryEvents, e = cgfmt.ParseMemoryEvents(b)
		return e
	}); le != nil {
		return nil, le
	}
	if le := rp(FileMemoryMax, func(b []byte) (e error) {
		s.Parsed.MemoryMaxBytes, s.Parsed.MemoryLimited, e = cgfmt.ParseMemoryMax(b)
		return e
	}); le != nil {
		return nil, le
	}
	if le := rp(FileMemPressure, func(b []byte) (e error) {
		s.Parsed.MemPressure, e = cgfmt.ParsePSI(b, "memory")
		return e
	}); le != nil {
		return nil, le
	}
	if contains(names, FileMemoryOOMGrp) {
		var v bool
		if le := rp(FileMemoryOOMGrp, func(b []byte) (e error) { v, e = cgfmt.ParseMemoryOOMGroup(b); return e }); le != nil {
			return nil, le
		}
		s.Parsed.OOMGroup = &v
	}
	if contains(names, FilePIDsCurrent) {
		var v uint64
		if le := rp(FilePIDsCurrent, func(b []byte) (e error) { v, e = cgfmt.ParsePIDsCurrent(b); return e }); le != nil {
			return nil, le
		}
		s.Parsed.PIDsCurrent = &v
	}
	if contains(names, FilePIDsEvents) {
		var v uint64
		if le := rp(FilePIDsEvents, func(b []byte) (e error) { v, e = cgfmt.ParsePIDsEvents(b); return e }); le != nil {
			return nil, le
		}
		s.Parsed.PIDsMaxEvents = &v
	}
	if contains(names, FileInstanceID) {
		var id string
		if le := rp(FileInstanceID, func(b []byte) (e error) { id, e = cgfmt.ParseInstanceID(b); return e }); le != nil {
			return nil, le
		}
		s.Parsed.InstanceID = &id
	}
	sort.Slice(s.Files, func(i, j int) bool { return s.Files[i].Name < s.Files[j].Name })
	return s, nil
}

func readFileNames(dir string) ([]string, error) {
	es, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range es {
		if e.IsDir() {
			return nil, fmt.Errorf("unexpected nested directory %q", e.Name())
		}
		out = append(out, e.Name())
	}
	sort.Strings(out)
	return out, nil
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

// MatchesFixtureLayout reports whether path looks like our fixture root by
// checking it is a directory; content validation happens during LoadRoot.
func MatchesFixtureLayout(path string) bool {
	st, err := os.Stat(path)
	return err == nil && st.IsDir()
}

// Ensure strings import used (container names are escaped in SQL upstream).
var _ = strings.TrimSpace
