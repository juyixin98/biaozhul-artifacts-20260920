// Package fixture loads offline cgroup v2 samples from a fixture directory.
//
// Directory layout (anything outside this shape is an error):
//
//	<root>/containers/<container>/<instance>/samples/<NNNNNN>/
//	    timestamp         single integer, Unix seconds of the sample
//	    cpu.stat          cgroup v2 cpu.stat (see package cgroup)
//	    memory.current    bytes, single integer
//	    memory.max        bytes or "max"
//	    memory.events     cgroup v2 memory.events
//	    cpu.pressure      PSI
//	    memory.pressure   PSI
//	<root>/containers/<container>/<instance>/exit.json   (optional)
//	    {"code": 0, "reason": "completed", "time": 1700000060}
//
// <container> and <instance> are directory names; the same container name may
// appear with several instance ids (container rebuilt/restarted). Sample
// directories are zero-padded sequence numbers; gaps mean lost samples.
//
// Missing individual files inside a sample do not abort loading: the
// corresponding fields stay nil and a warning is recorded on the sample.
// A missing or malformed timestamp is fatal for that sample (rates need a
// wall-clock delta), as is a malformed file that is present.
package fixture

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"cgroup-analyzer/internal/cgroup"
)

// Sample is one parsed sampling point of one container instance.
// Nil pointers mean the corresponding file was absent from the sample.
type Sample struct {
	Container string `json:"container"`
	Instance  string `json:"instance"`
	Seq       int    `json:"seq"`
	Timestamp int64  `json:"timestamp"` // Unix seconds

	CPU         *cgroup.CPUStat      `json:"cpu,omitempty"`
	MemCurrent  *uint64              `json:"mem_current_bytes,omitempty"`
	MemMax      *uint64              `json:"mem_max_bytes,omitempty"` // nil when "max" or file absent
	MemMaxSet   bool                 `json:"mem_max_present"`         // memory.max file existed
	MemEvents   *cgroup.MemoryEvents `json:"memory_events,omitempty"`
	CPUPressure *cgroup.Pressure     `json:"cpu_pressure,omitempty"`
	MemPressure *cgroup.Pressure     `json:"memory_pressure,omitempty"`

	Warnings []string `json:"warnings,omitempty"`
}

// ExitInfo is the optional exit.json record of an instance.
type ExitInfo struct {
	Code   int    `json:"code"`
	Reason string `json:"reason"`
	Time   int64  `json:"time"` // Unix seconds
}

// Instance is one run of a container: an ordered list of samples plus an
// optional exit record.
type Instance struct {
	Container string    `json:"container"`
	Instance  string    `json:"instance"`
	Samples   []Sample  `json:"samples"`
	Exit      *ExitInfo `json:"exit,omitempty"`
}

// Load walks root/containers and returns all instances, sorted by container
// name, instance id, and sample sequence.
func Load(root string) ([]Instance, error) {
	base := filepath.Join(root, "containers")
	entries, err := os.ReadDir(base)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", base, err)
	}
	var out []Instance
	for _, c := range entries {
		if !c.IsDir() {
			return nil, fmt.Errorf("%s: unexpected non-directory entry %q", base, c.Name())
		}
		insts, err := loadContainer(filepath.Join(base, c.Name()), c.Name())
		if err != nil {
			return nil, err
		}
		out = append(out, insts...)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Container != out[j].Container {
			return out[i].Container < out[j].Container
		}
		return out[i].Instance < out[j].Instance
	})
	return out, nil
}

func loadContainer(dir, name string) ([]Instance, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", dir, err)
	}
	var out []Instance
	for _, e := range entries {
		if !e.IsDir() {
			return nil, fmt.Errorf("%s: unexpected non-directory entry %q", dir, e.Name())
		}
		inst, err := loadInstance(filepath.Join(dir, e.Name()), name, e.Name())
		if err != nil {
			return nil, err
		}
		out = append(out, inst)
	}
	return out, nil
}

func loadInstance(dir, container, instance string) (Instance, error) {
	inst := Instance{Container: container, Instance: instance}

	// Optional exit record.
	exitPath := filepath.Join(dir, "exit.json")
	if data, err := os.ReadFile(exitPath); err == nil {
		var ei ExitInfo
		if err := json.Unmarshal(data, &ei); err != nil {
			return inst, fmt.Errorf("%s: invalid exit.json: %w", exitPath, err)
		}
		inst.Exit = &ei
	} else if !os.IsNotExist(err) {
		return inst, fmt.Errorf("read %s: %w", exitPath, err)
	}

	samplesDir := filepath.Join(dir, "samples")
	entries, err := os.ReadDir(samplesDir)
	if err != nil {
		return inst, fmt.Errorf("read %s: %w", samplesDir, err)
	}
	for _, e := range entries {
		if !e.IsDir() {
			return inst, fmt.Errorf("%s: unexpected non-directory entry %q", samplesDir, e.Name())
		}
		seq, err := strconv.Atoi(e.Name())
		if err != nil {
			return inst, fmt.Errorf("%s: sample dir %q is not a sequence number", samplesDir, e.Name())
		}
		s, err := loadSample(filepath.Join(samplesDir, e.Name()), container, instance, seq)
		if err != nil {
			return inst, err
		}
		inst.Samples = append(inst.Samples, s)
	}
	sort.Slice(inst.Samples, func(i, j int) bool { return inst.Samples[i].Seq < inst.Samples[j].Seq })
	return inst, nil
}

func loadSample(dir, container, instance string, seq int) (Sample, error) {
	s := Sample{Container: container, Instance: instance, Seq: seq}

	// timestamp is mandatory: rates are computed over wall-clock deltas.
	tsRaw, err := os.ReadFile(filepath.Join(dir, "timestamp"))
	if err != nil {
		return s, fmt.Errorf("%s: timestamp: %w", dir, err)
	}
	ts, err := strconv.ParseInt(strings.TrimSpace(string(tsRaw)), 10, 64)
	if err != nil {
		return s, fmt.Errorf("%s: timestamp: expected Unix seconds integer: %w", dir, err)
	}
	s.Timestamp = ts

	readOptional := func(name string) (string, bool, error) {
		data, err := os.ReadFile(filepath.Join(dir, name))
		if os.IsNotExist(err) {
			return "", false, nil
		}
		if err != nil {
			return "", false, fmt.Errorf("%s: %s: %w", dir, name, err)
		}
		return string(data), true, nil
	}
	missing := func(name string) {
		s.Warnings = append(s.Warnings, fmt.Sprintf("missing file %q in sample %06d", name, seq))
	}

	if content, ok, err := readOptional("cpu.stat"); err != nil {
		return s, err
	} else if !ok {
		missing("cpu.stat")
	} else {
		v, w, err := cgroup.ParseCPUStat(content)
		if err != nil {
			return s, fmt.Errorf("%s: %w", dir, err)
		}
		s.CPU = &v
		s.Warnings = append(s.Warnings, w...)
	}

	if content, ok, err := readOptional("memory.current"); err != nil {
		return s, err
	} else if !ok {
		missing("memory.current")
	} else {
		v, err := cgroup.ParseUint64File(content, "memory.current")
		if err != nil {
			return s, fmt.Errorf("%s: %w", dir, err)
		}
		s.MemCurrent = &v
	}

	if content, ok, err := readOptional("memory.max"); err != nil {
		return s, err
	} else if !ok {
		missing("memory.max")
	} else {
		v, unlimited, err := cgroup.ParseMemoryMax(content)
		if err != nil {
			return s, fmt.Errorf("%s: %w", dir, err)
		}
		s.MemMaxSet = true
		if !unlimited {
			s.MemMax = &v
		}
	}

	if content, ok, err := readOptional("memory.events"); err != nil {
		return s, err
	} else if !ok {
		missing("memory.events")
	} else {
		v, w, err := cgroup.ParseMemoryEvents(content)
		if err != nil {
			return s, fmt.Errorf("%s: %w", dir, err)
		}
		s.MemEvents = &v
		s.Warnings = append(s.Warnings, w...)
	}

	if content, ok, err := readOptional("cpu.pressure"); err != nil {
		return s, err
	} else if !ok {
		missing("cpu.pressure")
	} else {
		v, w, err := cgroup.ParsePressure(content, "cpu.pressure")
		if err != nil {
			return s, fmt.Errorf("%s: %w", dir, err)
		}
		s.CPUPressure = &v
		s.Warnings = append(s.Warnings, w...)
	}

	if content, ok, err := readOptional("memory.pressure"); err != nil {
		return s, err
	} else if !ok {
		missing("memory.pressure")
	} else {
		v, w, err := cgroup.ParsePressure(content, "memory.pressure")
		if err != nil {
			return s, fmt.Errorf("%s: %w", dir, err)
		}
		s.MemPressure = &v
		s.Warnings = append(s.Warnings, w...)
	}

	return s, nil
}
