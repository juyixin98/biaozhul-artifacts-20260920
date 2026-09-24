// Package cganalyze turns ordered cgroup samples into per-interval rates and
// resource events.
//
// Rate model
//
//	All cgroup counters in scope (cpu.stat, memory.events, pids.events and PSI
//	"total" fields) are cumulative since cgroup creation. Rates are finite
//	differences between two adjacent valid samples divided by the real wall
//	clock interval taken from their timestamp directory names:
//
//	    rate = (curr - prev) / dt
//
//	A counter that went backwards yields NO rate (null + reason), never a
//	negative number. A changed instance.id invalidates every rate for the
//	interval because the two endpoints are different containers.
//
// Units are fixed and named in every output field:
//
//	cpu usage counters ... microseconds (1e-6 s)
//	cpu_*_cores         ... Δcounter-µs / Δwall-µs  (1.0 == one full core)
//	*_per_second        ... events per wall second
//	*_stall_fraction    ... Δstall-µs / Δwall-µs, range [0, ~1]
//	memory              ... bytes; mib values use 1 MiB = 1048576 bytes
//
// Events are reported with the counter deltas and file hashes that prove them.
// When the evidence does not distinguish two outcomes, the response says so
// instead of guessing.
package cganalyze

import (
	"sort"
	"time"

	"cresnap/pkg/cgfmt"
	"cresnap/pkg/cgsample"
)

// Interval status, most severe first.
const (
	StatusOK             = "ok"
	StatusGap            = "gap"
	StatusCounterReset   = "counter_reset"
	StatusInstanceRebuilt = "instance_rebuilt"
)

// Event kinds.
const (
	EventOOMKill           = "oom_kill"            // memory.events oom_kill increased
	EventOOMDetected       = "oom_event"           // oom increased, oom_kill did not
	EventMemoryLimit       = "memory_limit_reached" // max counter increased, no OOM kill
	EventProcessExited     = "process_exited"      // pids fell to 0, same instance, no OOM
	EventInstanceRebuilt   = "instance_rebuilt"    // instance.id changed
)

// Rate wraps a finite-difference result.
type Rate struct {
	Value  *float64 `json:"value"`
	Reason string   `json:"null_reason,omitempty"` // "counter_reset" | "instance_rebuilt" | "counter_absent"
	Delta  *int64   `json:"delta,omitempty"`      // signed raw delta when computable
}

func okRate(delta uint64, dtSec float64) Rate {
	v := float64(delta) / dtSec
	d := int64(delta)
	return Rate{Value: &v, Delta: &d}
}

func nullRate(reason string) Rate { return Rate{Reason: reason} }

// CPURates are the derived cpu.stat rates for one interval.
type CPURates struct {
	UsageCores                 Rate `json:"usage_cores"`
	UserCores                  Rate `json:"user_cores,omitempty"`
	SystemCores                Rate `json:"system_cores,omitempty"`
	PeriodsPerSecond           Rate `json:"periods_per_second,omitempty"`
	ThrottledEventsPerSecond   Rate `json:"throttled_events_per_second,omitempty"`
	ThrottledFraction          Rate `json:"throttled_fraction,omitempty"` // throttled_usec / wall
}

// CounterDelta reports a cumulative-counter change and its event rate.
type CounterDelta struct {
	From        uint64  `json:"from"`
	To          uint64  `json:"to"`
	Delta       *int64  `json:"delta"`
	PerSecond   *float64 `json:"per_second"`
	NullReason  string  `json:"null_reason,omitempty"`
}

// MemoryEventsDelta covers all five memory.events counters.
type MemoryEventsDelta struct {
	Low     CounterDelta `json:"low"`
	High    CounterDelta `json:"high"`
	Max     CounterDelta `json:"max"`
	OOM     CounterDelta `json:"oom"`
	OOMKill CounterDelta `json:"oom_kill"`
}

// PressureRates carries stall-time fractions plus the end-of-interval PSI
// averages reported verbatim by the kernel (already exponentially weighted).
type PressureRates struct {
	CPUSomeStallFraction       Rate     `json:"cpu_some_stall_fraction"`
	MemorySomeStallFraction    Rate     `json:"memory_some_stall_fraction"`
	MemoryFullStallFraction    Rate     `json:"memory_full_stall_fraction"`
	ToCPUSomeAvg10             float64  `json:"to_cpu_some_avg10"`
	ToCPUSomeAvg60             float64  `json:"to_cpu_some_avg60"`
	ToCPUSomeAvg300            float64  `json:"to_cpu_some_avg300"`
	ToMemSomeAvg10             float64  `json:"to_memory_some_avg10"`
	ToMemSomeAvg60             float64  `json:"to_memory_some_avg60"`
	ToMemSomeAvg300            float64  `json:"to_memory_some_avg300"`
	ToMemFullAvg10             float64  `json:"to_mem_full_avg10"`
	ToMemFullAvg60             float64  `json:"to_mem_full_avg60"`
	ToMemFullAvg300            float64  `json:"to_mem_full_avg300"`
}

// MemoryState captures the endpoint gauges for one interval.
type MemoryState struct {
	FromBytes  uint64  `json:"from_bytes"`
	ToBytes    uint64  `json:"to_bytes"`
	FromMiB    float64 `json:"from_mib"`
	ToMiB      float64 `json:"to_mib"`
	LimitBytes *uint64 `json:"limit_bytes"`
	LimitMiB   *float64 `json:"limit_mib"`
	Limited    bool    `json:"has_limit"` // false when memory.max == "max"
	ToUtilPct  *float64 `json:"to_utilization_pct,omitempty"`
}

// SourceFile binds one parsed file at one endpoint to its digest.
type SourceFile struct {
	Sample string `json:"sample_dir"`
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
	Bytes  int64  `json:"bytes"`
}

// Event is one classified resource event with the evidence behind it.
type Event struct {
	Kind       string                 `json:"kind"`
	At         string                 `json:"at"` // timestamp of the interval end sample
	Container  string                 `json:"container"`
	Summary    string                 `json:"summary"`
	Evidence   map[string]interface{} `json:"evidence"`
	Sources    []SourceFile           `json:"sources"`
}

// PIDsDelta is present when both endpoints have pids.current.
type PIDsDelta struct {
	From       uint64 `json:"from"`
	To         uint64 `json:"to"`
	MaxEvents  *CounterDelta `json:"pids_events_max,omitempty"`
}

// Interval is the full analysis between two adjacent samples.
type Interval struct {
	Container      string             `json:"container"`
	From           string             `json:"from"`
	To             string             `json:"to"`
	FromDir        string             `json:"from_sample_dir"`
	ToDir          string             `json:"to_sample_dir"`
	WallSeconds    float64            `json:"wall_seconds"`
	Status         string             `json:"status"`
	MissingSamples int                `json:"missing_samples"`
	ResetCounters  []string           `json:"reset_counters,omitempty"`
	InstanceFrom   *string            `json:"instance_from"`
	InstanceTo     *string            `json:"instance_to"`
	CPU            CPURates           `json:"cpu"`
	Memory         MemoryState        `json:"memory"`
	MemoryEvents   MemoryEventsDelta  `json:"memory_events"`
	Pressure       PressureRates      `json:"pressure"`
	PIDs           *PIDsDelta         `json:"pids,omitempty"`
	Events         []Event            `json:"events"`
}

// SamplePayload is the stored/serialized view of one raw sample (including
// the first, baseline sample that has no preceding interval).
type SamplePayload struct {
	Seq            int                `json:"seq"`
	DirName        string             `json:"sample_dir"`
	Timestamp      string             `json:"timestamp"`
	InstanceID     *string            `json:"instance_id"`
	CPUStat        cgfmt.CPUStat      `json:"cpu_stat"`
	CPUPressure    cgfmt.PSI          `json:"cpu_pressure"`
	MemoryCurrent  uint64             `json:"memory_current_bytes"`
	MemoryEvents   cgfmt.MemoryEvents `json:"memory_events"`
	MemoryMaxBytes uint64             `json:"memory_max_bytes"`
	MemoryLimited  bool               `json:"memory_has_limit"`
	MemPressure    cgfmt.PSI          `json:"memory_pressure"`
	PIDsCurrent    *uint64            `json:"pids_current"`
	PIDsMaxEvents  *uint64            `json:"pids_events_max"`
	OOMGroup       *bool              `json:"memory_oom_group"`
	Files          []cgsample.FileHash `json:"files"`

	// Convenience columns mirrored into the DB.
	CPUUsageUsec uint64 `json:"-"`
}

// ContainerReport is the timeline for one container.
type ContainerReport struct {
	Container      string           `json:"container"`
	NominalSeconds float64          `json:"nominal_seconds"` // smallest observed interval
	SampleCount    int              `json:"sample_count"`
	Samples        []string         `json:"sample_dirs"`
	SamplePayloads []SamplePayload  `json:"-"`
	Intervals      []*Interval      `json:"intervals"`
}

// Report is the complete analysis output.
type Report struct {
	Root           string                     `json:"fixture_root"`
	Containers     []*ContainerReport         `json:"containers"`
	LoadErrors     []cgsample.LoadError       `json:"load_errors"`
	IgnoredSamples int                        `json:"ignored_sample_count"`
}


// Analyze builds a report from loaded samples.
func Analyze(root string, loaded map[string][]*cgsample.Sample, loadErrors []cgsample.LoadError) *Report {
	r := &Report{Root: root, LoadErrors: loadErrors, IgnoredSamples: len(loadErrors)}
	names := make([]string, 0, len(loaded))
	for c := range loaded {
		names = append(names, c)
	}
	sort.Strings(names)
	for _, name := range names {
		r.Containers = append(r.Containers, analyzeContainer(name, loaded[name]))
	}
	return r
}

const mib = 1048576.0

func mibPtr(b uint64) *float64 { v := float64(b) / mib; return &v }

func analyzeContainer(name string, ss []*cgsample.Sample) *ContainerReport {
	cr := &ContainerReport{Container: name, SampleCount: len(ss)}
	for i, s := range ss {
		cr.Samples = append(cr.Samples, s.DirName)
		cr.SamplePayloads = append(cr.SamplePayloads, SamplePayload{
			Seq:            i,
			DirName:        s.DirName,
			Timestamp:      s.Time.UTC().Format(time.RFC3339),
			InstanceID:     s.Parsed.InstanceID,
			CPUStat:        s.Parsed.CPUStat,
			CPUPressure:    s.Parsed.CPUPressure,
			MemoryCurrent:  s.Parsed.MemoryCurrent,
			MemoryEvents:   s.Parsed.MemoryEvents,
			MemoryMaxBytes: s.Parsed.MemoryMaxBytes,
			MemoryLimited:  s.Parsed.MemoryLimited,
			MemPressure:    s.Parsed.MemPressure,
			PIDsCurrent:    s.Parsed.PIDsCurrent,
			PIDsMaxEvents:  s.Parsed.PIDsMaxEvents,
			OOMGroup:       s.Parsed.OOMGroup,
			Files:          s.Files,
			CPUUsageUsec:   s.Parsed.CPUStat.UsageUsec,
		})
	}
	if len(ss) < 2 {
		return cr
	}
	// Nominal cadence = smallest observed wall gap; used only to flag missing
	// samples, never to fabricate data.
	nominal := 0.0
	for i := 1; i < len(ss); i++ {
		d := ss[i].Time.Sub(ss[i-1].Time).Seconds()
		if nominal == 0 || d < nominal {
			nominal = d
		}
	}
	cr.NominalSeconds = nominal
	for i := 1; i < len(ss); i++ {
		cr.Intervals = append(cr.Intervals, analyzeInterval(ss[i-1], ss[i], nominal))
	}
	return cr
}

func counterDelta(prev, curr uint64, dtSec float64, rebuilt bool) CounterDelta {
	cd := CounterDelta{From: prev, To: curr}
	if rebuilt {
		cd.NullReason = "instance_rebuilt"
		return cd
	}
	d := int64(curr - prev)
	cd.Delta = &d
	if curr < prev {
		cd.NullReason = "counter_reset"
		cd.Delta = nil
		return cd
	}
	dd := d
	p := float64(dd) / dtSec
	cd.PerSecond = &p
	return cd
}

// microCounterRate computes a microseconds-counter finite difference.
func microCounterRate(prev, curr uint64, dtSec float64, rebuilt bool, reset bool) Rate {
	if rebuilt {
		return nullRate("instance_rebuilt")
	}
	if reset {
		return nullRate("counter_reset")
	}
	return okRate(curr-prev, dtSec*1e6) // value = Δµs/Δwallµs
}

func eventRate(prev, curr *uint64, dtSec float64, rebuilt, resetSeen bool) Rate {
	if prev == nil || curr == nil {
		return nullRate("counter_absent")
	}
	if rebuilt {
		return nullRate("instance_rebuilt")
	}
	if *curr < *prev {
		return nullRate("counter_reset")
	}
	return okRate(*curr-*prev, dtSec)
}

func analyzeInterval(a, b *cgsample.Sample, nominal float64) *Interval {
	dt := b.Time.Sub(a.Time).Seconds()
	iv := &Interval{
		Container:   b.Container,
		From:        a.Time.UTC().Format(time.RFC3339),
		To:          b.Time.UTC().Format(time.RFC3339),
		FromDir:     a.DirName,
		ToDir:       b.DirName,
		WallSeconds: dt,
		Status:      StatusOK,
		InstanceFrom: a.Parsed.InstanceID,
		InstanceTo:   b.Parsed.InstanceID,
	}

	rebuilt := a.Parsed.InstanceID != nil && b.Parsed.InstanceID != nil &&
		*a.Parsed.InstanceID != *b.Parsed.InstanceID

	// Gap / missing-sample detection against the observed cadence.
	if nominal > 0 && dt > nominal*1.5+1e-9 {
		iv.Status = StatusGap
		iv.MissingSamples = int(dt/nominal+0.5) - 1
	}
	if rebuilt {
		iv.Status = StatusInstanceRebuilt
	}

	// ---- cpu.stat ----
	cpuReset := !rebuilt && b.Parsed.CPUStat.UsageUsec < a.Parsed.CPUStat.UsageUsec
	if cpuReset {
		iv.ResetCounters = append(iv.ResetCounters, "cpu.stat/usage_usec")
	}
	iv.CPU.UsageCores = microCounterRate(a.Parsed.CPUStat.UsageUsec, b.Parsed.CPUStat.UsageUsec, dt, rebuilt, cpuReset)
	iv.CPU.UserCores = optMicroPair(a.Parsed.CPUStat.UserUsec, b.Parsed.CPUStat.UserUsec, dt, rebuilt, "cpu.stat/user_usec", iv)
	iv.CPU.SystemCores = optMicroPair(a.Parsed.CPUStat.SystemUsec, b.Parsed.CPUStat.SystemUsec, dt, rebuilt, "cpu.stat/system_usec", iv)
	iv.CPU.PeriodsPerSecond = optEventPair(a.Parsed.CPUStat.NrPeriods, b.Parsed.CPUStat.NrPeriods, dt, rebuilt, "cpu.stat/nr_periods", iv)
	iv.CPU.ThrottledEventsPerSecond = optEventPair(a.Parsed.CPUStat.NrThrottled, b.Parsed.CPUStat.NrThrottled, dt, rebuilt, "cpu.stat/nr_throttled", iv)
	iv.CPU.ThrottledFraction = optMicroPair(a.Parsed.CPUStat.ThrottledUsec, b.Parsed.CPUStat.ThrottledUsec, dt, rebuilt, "cpu.stat/throttled_usec", iv)

	// ---- memory gauges ----
	mem := MemoryState{
		FromBytes: a.Parsed.MemoryCurrent,
		ToBytes:   b.Parsed.MemoryCurrent,
		FromMiB:   float64(a.Parsed.MemoryCurrent) / mib,
		ToMiB:     float64(b.Parsed.MemoryCurrent) / mib,
		Limited:   b.Parsed.MemoryLimited,
	}
	if b.Parsed.MemoryLimited {
		lb := b.Parsed.MemoryMaxBytes
		mem.LimitBytes = &lb
		mem.LimitMiB = mibPtr(lb)
		util := float64(b.Parsed.MemoryCurrent) / float64(lb) * 100
		mem.ToUtilPct = &util
	}
	iv.Memory = mem

	// ---- memory.events deltas ----
	me := func(p, c uint64, key string) CounterDelta {
		cd := counterDelta(p, c, dt, rebuilt)
		if cd.NullReason == "counter_reset" {
			iv.ResetCounters = append(iv.ResetCounters, "memory.events/"+key)
			if iv.Status == StatusOK || iv.Status == StatusGap {
				iv.Status = StatusCounterReset
			}
		}
		return cd
	}
	iv.MemoryEvents = MemoryEventsDelta{
		Low:     me(a.Parsed.MemoryEvents.Low, b.Parsed.MemoryEvents.Low, "low"),
		High:    me(a.Parsed.MemoryEvents.High, b.Parsed.MemoryEvents.High, "high"),
		Max:     me(a.Parsed.MemoryEvents.Max, b.Parsed.MemoryEvents.Max, "max"),
		OOM:     me(a.Parsed.MemoryEvents.OOM, b.Parsed.MemoryEvents.OOM, "oom"),
		OOMKill: me(a.Parsed.MemoryEvents.OOMKill, b.Parsed.MemoryEvents.OOMKill, "oom_kill"),
	}
	if cpuReset && !rebuilt && iv.Status != StatusGap {
		iv.Status = StatusCounterReset
	}
	if rebuilt {
		iv.Status = StatusInstanceRebuilt
	}

	// ---- PSI ----
	iv.Pressure = PressureRates{
		CPUSomeStallFraction:    psiRate(a.Parsed.CPUPressure.Some.Total, b.Parsed.CPUPressure.Some.Total, dt, rebuilt, "cpu.pressure/some.total", iv),
		MemorySomeStallFraction: psiRate(a.Parsed.MemPressure.Some.Total, b.Parsed.MemPressure.Some.Total, dt, rebuilt, "memory.pressure/some.total", iv),
		MemoryFullStallFraction: psiRate(a.Parsed.MemPressure.Full.Total, b.Parsed.MemPressure.Full.Total, dt, rebuilt, "memory.pressure/full.total", iv),
		ToCPUSomeAvg10:          b.Parsed.CPUPressure.Some.Avg10,
		ToCPUSomeAvg60:          b.Parsed.CPUPressure.Some.Avg60,
		ToCPUSomeAvg300:         b.Parsed.CPUPressure.Some.Avg300,
		ToMemSomeAvg10:          b.Parsed.MemPressure.Some.Avg10,
		ToMemSomeAvg60:          b.Parsed.MemPressure.Some.Avg60,
		ToMemSomeAvg300:         b.Parsed.MemPressure.Some.Avg300,
		ToMemFullAvg10:          b.Parsed.MemPressure.Full.Avg10,
		ToMemFullAvg60:          b.Parsed.MemPressure.Full.Avg60,
		ToMemFullAvg300:         b.Parsed.MemPressure.Full.Avg300,
	}

	// ---- pids ----
	if a.Parsed.PIDsCurrent != nil && b.Parsed.PIDsCurrent != nil {
		pd := &PIDsDelta{From: *a.Parsed.PIDsCurrent, To: *b.Parsed.PIDsCurrent}
		if a.Parsed.PIDsMaxEvents != nil && b.Parsed.PIDsMaxEvents != nil {
			cd := counterDelta(*a.Parsed.PIDsMaxEvents, *b.Parsed.PIDsMaxEvents, dt, rebuilt)
			if cd.NullReason == "counter_reset" {
				iv.ResetCounters = append(iv.ResetCounters, "pids.events/max")
			}
			pd.MaxEvents = &cd
		}
		iv.PIDs = pd
	}

	// ---- events ----
	classifyEvents(iv, a, b, rebuilt)
	if rebuilt {
		iv.Events = append(iv.Events, rebuildEvent(a, b))
	}
	sort.Strings(iv.ResetCounters)
	return iv
}

func optMicroPair(prev, curr *uint64, dt float64, rebuilt bool, key string, iv *Interval) Rate {
	if prev == nil || curr == nil {
		return nullRate("counter_absent")
	}
	reset := !rebuilt && *curr < *prev
	if reset {
		iv.ResetCounters = appendUnique(iv.ResetCounters, key)
		if iv.Status == StatusOK || iv.Status == StatusGap {
			iv.Status = StatusCounterReset
		}
	}
	return microCounterRate(*prev, *curr, dt, rebuilt, reset)
}

func optEventPair(prev, curr *uint64, dt float64, rebuilt bool, key string, iv *Interval) Rate {
	if prev == nil || curr == nil {
		return nullRate("counter_absent")
	}
	reset := !rebuilt && *curr < *prev
	if reset {
		iv.ResetCounters = appendUnique(iv.ResetCounters, key)
		if iv.Status == StatusOK || iv.Status == StatusGap {
			iv.Status = StatusCounterReset
		}
	}
	return eventRate(prev, curr, dt, rebuilt, reset)
}

func psiRate(prev, curr uint64, dt float64, rebuilt bool, key string, iv *Interval) Rate {
	if rebuilt {
		return nullRate("instance_rebuilt")
	}
	if curr < prev {
		iv.ResetCounters = appendUnique(iv.ResetCounters, key)
		if iv.Status == StatusOK || iv.Status == StatusGap {
			iv.Status = StatusCounterReset
		}
		return nullRate("counter_reset")
	}
	return okRate(curr-prev, dt*1e6)
}

func appendUnique(xs []string, s string) []string {
	for _, x := range xs {
		if x == s {
			return xs
		}
	}
	return append(xs, s)
}

// classifyEvents encodes the OOM / limit / exit decision table. All branches
// are driven strictly by counter deltas and pids; nothing is inferred from
// memory size alone.
func classifyEvents(iv *Interval, a, b *cgsample.Sample, rebuilt bool) {
	dKill := iv.MemoryEvents.OOMKill
	dOOM := iv.MemoryEvents.OOM
	dMax := iv.MemoryEvents.Max
	inc := func(cd CounterDelta) (bool, int64) {
		return cd.Delta != nil && *cd.Delta > 0, func() int64 {
			if cd.Delta != nil {
				return *cd.Delta
			}
			return 0
		}()
	}
	killUp, killN := inc(dKill)
	oomUp, oomN := inc(dOOM)
	maxUp, maxN := inc(dMax)

	switch {
	case killUp:
		var oomGroup interface{}
		if b.Parsed.OOMGroup != nil {
			oomGroup = *b.Parsed.OOMGroup
		}
		iv.Events = append(iv.Events, Event{
			Kind:      EventOOMKill,
			At:        iv.To,
			Container: b.Container,
			Summary:   "kernel OOM killer killed a task: memory.events oom_kill increased",
			Evidence: map[string]interface{}{
				"oom_kill_delta":           killN,
				"oom_delta":                oomN,
				"max_delta":                maxN,
				"memory_events_from":       a.Parsed.MemoryEvents,
				"memory_events_to":         b.Parsed.MemoryEvents,
				"memory_current_to_bytes":  b.Parsed.MemoryCurrent,
				"memory_max_bytes":         limitEvidence(b),
				"memory_oom_group_enabled": oomGroup,
				"cpu_usage_cores_interval": iv.CPU.UsageCores.Value,
				"instance_from":            a.Parsed.InstanceID,
				"instance_to":              b.Parsed.InstanceID,
			},
			Sources: sourcesFor(a, b, cgsample.FileMemoryEvents, cgsample.FileMemoryCurrent,
				cgsample.FileMemoryMax, cgsample.FileCPUStat, cgsample.FileMemoryOOMGrp),
		})
	case oomUp:
		iv.Events = append(iv.Events, Event{
			Kind:      EventOOMDetected,
			At:        iv.To,
			Container: b.Container,
			Summary:   "OOM condition detected (oom counter up) but no killed task recorded (oom_kill unchanged)",
			Evidence: map[string]interface{}{
				"oom_delta":          oomN,
				"oom_kill_delta":     int64(0),
				"max_delta":          maxN,
				"memory_events_from": a.Parsed.MemoryEvents,
				"memory_events_to":   b.Parsed.MemoryEvents,
				"interpretation":     "kernel entered OOM handling; oom_kill did not advance (e.g. killed task in a sibling cgroup or oom_group accounting)",
				"instance_from":      a.Parsed.InstanceID,
				"instance_to":        b.Parsed.InstanceID,
			},
			Sources: sourcesFor(a, b, cgsample.FileMemoryEvents, cgsample.FileMemoryMax),
		})
	case maxUp && !rebuilt:
		iv.Events = append(iv.Events, Event{
			Kind:      EventMemoryLimit,
			At:        iv.To,
			Container: b.Container,
			Summary:   "memory.max was hit: allocation reclaimed/stalled at the hard limit with no OOM kill recorded",
			Evidence: map[string]interface{}{
				"max_delta":               maxN,
				"oom_delta":               oomN,
				"oom_kill_delta":          int64(0),
				"memory_events_from":      a.Parsed.MemoryEvents,
				"memory_events_to":        b.Parsed.MemoryEvents,
				"memory_current_to_bytes": b.Parsed.MemoryCurrent,
				"memory_max_bytes":        limitEvidence(b),
				"memory_high_delta":       deltaOrNil(iv.MemoryEvents.High),
				"distinction":             "max counter advanced while oom/oom_kill did not: limit reached without kill",
			},
			Sources: sourcesFor(a, b, cgsample.FileMemoryEvents, cgsample.FileMemoryCurrent, cgsample.FileMemoryMax),
		})
	}

	// Normal process exit: pids visible at both ends, fell to zero, counters
	// consistent with the SAME instance, and no OOM kill in the interval.
	if iv.PIDs != nil && iv.PIDs.To == 0 && iv.PIDs.From > 0 && !killUp && !rebuilt {
		usageCont := iv.CPU.UsageCores.Value
		ev := Event{
			Kind:      EventProcessExited,
			At:        iv.To,
			Container: b.Container,
			Summary:   "all processes exited on their own: pids.current reached 0 with monotonic counters and no OOM kill",
			Evidence: map[string]interface{}{
				"pids_from":                iv.PIDs.From,
				"pids_to":                  iv.PIDs.To,
				"memory_current_from_bytes": a.Parsed.MemoryCurrent,
				"memory_current_to_bytes":  b.Parsed.MemoryCurrent,
				"oom_kill_delta":           deltaOrNil(iv.MemoryEvents.OOMKill),
				"oom_delta":                deltaOrNil(iv.MemoryEvents.OOM),
				"max_delta":                deltaOrNil(iv.MemoryEvents.Max),
				"same_instance":            sameInstance(a, b),
				"cpu_usage_delta_usec":     iv.CPU.UsageCores.Delta,
				"cpu_usage_cores":          usageCont,
				"wall_seconds":             iv.WallSeconds,
				"exit_code_known":          false,
				"note":                     "exit code is not observable in cgroup counters; reported as exit, not success/failure",
			},
			Sources: sourcesFor(a, b, cgsample.FilePIDsCurrent, cgsample.FileMemoryCurrent,
				cgsample.FileMemoryEvents, cgsample.FileCPUStat),
		}
		iv.Events = append(iv.Events, ev)
	}
}

func rebuildEvent(a, b *cgsample.Sample) Event {
	return Event{
		Kind:      EventInstanceRebuilt,
		At:        b.Time.UTC().Format(time.RFC3339),
		Container: b.Container,
		Summary:   "instance.id changed: a different container instance reused this name; interval rates suppressed",
		Evidence: map[string]interface{}{
			"instance_from":         a.Parsed.InstanceID,
			"instance_to":           b.Parsed.InstanceID,
			"cpu_usage_usec_from":   a.Parsed.CPUStat.UsageUsec,
			"cpu_usage_usec_to":     b.Parsed.CPUStat.UsageUsec,
			"rates_computed":        false,
			"reason":                "cumulative counters restart at 0 for a new cgroup; differences across the boundary are meaningless",
		},
		Sources: sourcesFor(a, b, cgsample.FileInstanceID, cgsample.FileCPUStat),
	}
}

func sameInstance(a, b *cgsample.Sample) bool {
	if a.Parsed.InstanceID == nil || b.Parsed.InstanceID == nil {
		return true // no identity sentinel: counter continuity is the evidence
	}
	return *a.Parsed.InstanceID == *b.Parsed.InstanceID
}

func deltaOrNil(cd CounterDelta) *int64 { return cd.Delta }

func limitEvidence(b *cgsample.Sample) interface{} {
	if !b.Parsed.MemoryLimited {
		return "max (unlimited)"
	}
	return b.Parsed.MemoryMaxBytes
}

// sourcesFor collects digests of the named files present at both endpoints.
func sourcesFor(a, b *cgsample.Sample, names ...string) []SourceFile {
	var out []SourceFile
	add := func(s *cgsample.Sample) {
		want := map[string]bool{}
		for _, n := range names {
			want[n] = true
		}
		for _, f := range s.Files {
			if want[f.Name] {
				out = append(out, SourceFile{Sample: s.DirName, Name: f.Name, SHA256: f.SHA256, Bytes: f.Bytes})
			}
		}
	}
	add(a)
	add(b)
	sort.Slice(out, func(i, j int) bool {
		if out[i].Sample != out[j].Sample {
			return out[i].Sample < out[j].Sample
		}
		return out[i].Name < out[j].Name
	})
	return out
}
