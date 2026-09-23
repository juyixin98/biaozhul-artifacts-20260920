// Package analysis computes per-interval rates and resource events from a
// sequence of offline cgroup v2 samples.
//
// Units and conventions:
//
//   - CPU rate: delta(cpu.stat usage_usec) / wall-clock delta, in cores
//     (1.0 = one fully busy CPU). usage_usec is microseconds, so
//     cores = delta_usec / (dt_seconds * 1e6).
//   - Throttle ratio: delta(throttled_usec) / (dt_seconds * 1e6), same unit.
//   - PSI stall rate: delta(pressure total) / dt, in microseconds of stall
//     per second (== percent / 100 when multiplied by 100).
//   - Memory: bytes as sampled; usage ratio = memory.current / memory.max.
//   - Sample interval: taken from each sample's own timestamp file, never
//     assumed constant; gaps in sequence numbers are reported, not hidden.
//
// Counter resets (container rebuilt, counter regression): when a cumulative
// counter decreases between samples, no rate is emitted for that interval
// (nil, never negative) and a counter_reset event is recorded with evidence.
package analysis

import (
	"fmt"

	"cgroup-analyzer/internal/fixture"
)

// IntervalRate describes the interval between two consecutive samples.
// Nil pointers mean "not computable" (missing file, counter reset, or
// non-positive time delta) — never a guessed value.
type IntervalRate struct {
	FromSeq          int      `json:"from_seq"`
	ToSeq            int      `json:"to_seq"`
	FromTime         int64    `json:"from_time"`
	ToTime           int64    `json:"to_time"`
	DurationSeconds  float64  `json:"duration_seconds"`
	CPUCores         *float64 `json:"cpu_cores,omitempty"`
	ThrottleRatio    *float64 `json:"throttle_ratio,omitempty"`
	PSICPUStallUsec  *float64 `json:"psi_cpu_stall_usec_per_sec,omitempty"`
	PSIMemStallUsec  *float64 `json:"psi_mem_stall_usec_per_sec,omitempty"`
	MemCurrentStart  *uint64  `json:"mem_current_start_bytes,omitempty"`
	MemCurrentEnd    *uint64  `json:"mem_current_end_bytes,omitempty"`
	MemLimitBytes    *uint64  `json:"mem_limit_bytes,omitempty"`
	MemUsageRatioEnd *float64 `json:"mem_usage_ratio_end,omitempty"`
	CounterReset     bool     `json:"counter_reset"`
	Gap              bool     `json:"gap"`
	MissingSamples   []int    `json:"missing_samples,omitempty"`
	Warnings         []string `json:"warnings,omitempty"`
}

// Event is a detected resource event. Evidence lists the concrete
// observations (file, counter values, sample ids) the conclusion rests on.
type Event struct {
	Type     string   `json:"type"` // oom_kill | memory_max_hit | counter_reset | sample_gap | normal_exit | abnormal_exit | incomplete_data
	Seq      int      `json:"seq"`  // sample where the event was observed (0 for instance-level)
	Time     int64    `json:"time"`
	Evidence []string `json:"evidence"`
}

// Report is the full analysis of one container instance.
type Report struct {
	Container   string         `json:"container"`
	Instance    string         `json:"instance"`
	SampleCount int            `json:"sample_count"`
	FirstTime   int64          `json:"first_time"`
	LastTime    int64          `json:"last_time"`
	Intervals   []IntervalRate `json:"intervals"`
	Events      []Event        `json:"events"`
	Warnings    []string       `json:"warnings,omitempty"`
}

func f64(v float64) *float64 { return &v }

// delta returns cur-prev and whether the counter did NOT reset.
func delta(prev, cur uint64) (uint64, bool) {
	if cur < prev {
		return 0, false
	}
	return cur - prev, true
}

// Analyze computes the report for one instance. Samples must be sorted by
// Seq (fixture.Load guarantees this).
func Analyze(inst fixture.Instance) Report {
	r := Report{Container: inst.Container, Instance: inst.Instance, SampleCount: len(inst.Samples)}
	if len(inst.Samples) == 0 {
		r.Warnings = append(r.Warnings, "instance has no samples")
		return r
	}
	r.FirstTime = inst.Samples[0].Timestamp
	r.LastTime = inst.Samples[len(inst.Samples)-1].Timestamp

	sawOomKill := false
	for i := 0; i+1 < len(inst.Samples); i++ {
		a, b := inst.Samples[i], inst.Samples[i+1]
		iv := IntervalRate{
			FromSeq: a.Seq, ToSeq: b.Seq,
			FromTime: a.Timestamp, ToTime: b.Timestamp,
		}
		dt := float64(b.Timestamp - a.Timestamp)
		iv.DurationSeconds = dt

		// Sequence gap: samples were lost between a and b.
		if b.Seq-a.Seq > 1 {
			iv.Gap = true
			for s := a.Seq + 1; s < b.Seq; s++ {
				iv.MissingSamples = append(iv.MissingSamples, s)
			}
			r.Events = append(r.Events, Event{
				Type: "sample_gap", Seq: b.Seq, Time: b.Timestamp,
				Evidence: []string{fmt.Sprintf("sample sequence jumps %06d -> %06d; %d sample(s) missing: %v",
					a.Seq, b.Seq, len(iv.MissingSamples), iv.MissingSamples)},
			})
		}

		if dt <= 0 {
			iv.Warnings = append(iv.Warnings,
				fmt.Sprintf("non-positive time delta (%ds) between samples %06d and %06d; rates not computable",
					b.Timestamp-a.Timestamp, a.Seq, b.Seq))
			r.Intervals = append(r.Intervals, iv)
			continue
		}

		// CPU usage rate with reset protection.
		if a.CPU != nil && b.CPU != nil {
			if d, ok := delta(a.CPU.UsageUsec, b.CPU.UsageUsec); ok {
				iv.CPUCores = f64(float64(d) / (dt * 1e6))
			} else {
				iv.CounterReset = true
				r.Events = append(r.Events, Event{
					Type: "counter_reset", Seq: b.Seq, Time: b.Timestamp,
					Evidence: []string{fmt.Sprintf(
						"cpu.stat usage_usec decreased %d -> %d between samples %06d and %06d (instance rebuilt or counter regressed); rate suppressed for this interval",
						a.CPU.UsageUsec, b.CPU.UsageUsec, a.Seq, b.Seq)},
				})
			}
			if d, ok := delta(a.CPU.ThrottledUsec, b.CPU.ThrottledUsec); ok {
				iv.ThrottleRatio = f64(float64(d) / (dt * 1e6))
			}
		} else {
			iv.Warnings = append(iv.Warnings, "cpu.stat missing in one of the samples; CPU rate not computable")
		}

		// PSI stall rates with reset protection.
		if a.CPUPressure != nil && b.CPUPressure != nil {
			if d, ok := delta(a.CPUPressure.Some.Total, b.CPUPressure.Some.Total); ok {
				iv.PSICPUStallUsec = f64(float64(d) / dt)
			}
		}
		if a.MemPressure != nil && b.MemPressure != nil {
			if d, ok := delta(a.MemPressure.Some.Total, b.MemPressure.Some.Total); ok {
				iv.PSIMemStallUsec = f64(float64(d) / dt)
			}
		}

		// Memory levels (gauges, not counters: reported as sampled).
		iv.MemCurrentStart, iv.MemCurrentEnd = a.MemCurrent, b.MemCurrent
		if b.MemMax != nil {
			iv.MemLimitBytes = b.MemMax
			if b.MemCurrent != nil && *b.MemMax > 0 {
				iv.MemUsageRatioEnd = f64(float64(*b.MemCurrent) / float64(*b.MemMax))
			}
		}

		// memory.events deltas -> OOM / limit-hit events with evidence.
		if a.MemEvents != nil && b.MemEvents != nil {
			if d, ok := delta(a.MemEvents.OomKill, b.MemEvents.OomKill); ok && d > 0 {
				sawOomKill = true
				ev := Event{
					Type: "oom_kill", Seq: b.Seq, Time: b.Timestamp,
					Evidence: []string{fmt.Sprintf(
						"memory.events oom_kill increased %d -> %d between samples %06d and %06d",
						a.MemEvents.OomKill, b.MemEvents.OomKill, a.Seq, b.Seq)},
				}
				if a.MemCurrent != nil && b.MemMax != nil {
					ev.Evidence = append(ev.Evidence, fmt.Sprintf(
						"memory.current at sample %06d was %d bytes against memory.max %d bytes",
						a.Seq, *a.MemCurrent, *b.MemMax))
				}
				r.Events = append(r.Events, ev)
			}
			if d, ok := delta(a.MemEvents.Max, b.MemEvents.Max); ok && d > 0 {
				r.Events = append(r.Events, Event{
					Type: "memory_max_hit", Seq: b.Seq, Time: b.Timestamp,
					Evidence: []string{fmt.Sprintf(
						"memory.events max increased %d -> %d between samples %06d and %06d (allocation hit memory.max %d time(s))",
						a.MemEvents.Max, b.MemEvents.Max, a.Seq, b.Seq, d)},
				})
			}
		}

		r.Intervals = append(r.Intervals, iv)
	}

	r.Events = append(r.Events, classifyExit(inst, sawOomKill)...)
	return r
}

// classifyExit determines how the instance ended, from evidence only.
func classifyExit(inst fixture.Instance, sawOomKill bool) []Event {
	last := inst.Samples[len(inst.Samples)-1]
	switch {
	case inst.Exit == nil:
		return []Event{{
			Type: "incomplete_data", Seq: last.Seq, Time: last.Timestamp,
			Evidence: []string{fmt.Sprintf(
				"samples stop at %06d (t=%d) with no exit.json; end state cannot be determined from available data",
				last.Seq, last.Timestamp)},
		}}
	case sawOomKill:
		return []Event{{
			Type: "oom_kill", Seq: last.Seq, Time: inst.Exit.Time,
			Evidence: []string{fmt.Sprintf(
				"exit.json records code=%d reason=%q, and memory.events oom_kill increased during the run; instance ended by OOM kill",
				inst.Exit.Code, inst.Exit.Reason)},
		}}
	case inst.Exit.Code == 0:
		ev := []string{fmt.Sprintf("exit.json records code=0 reason=%q at t=%d", inst.Exit.Reason, inst.Exit.Time),
			"memory.events oom_kill never increased during the run"}
		if last.MemEvents != nil {
			ev = append(ev, fmt.Sprintf("final memory.events oom_kill=%d oom=%d", last.MemEvents.OomKill, last.MemEvents.Oom))
		}
		return []Event{{Type: "normal_exit", Seq: last.Seq, Time: inst.Exit.Time, Evidence: ev}}
	default:
		return []Event{{
			Type: "abnormal_exit", Seq: last.Seq, Time: inst.Exit.Time,
			Evidence: []string{fmt.Sprintf(
				"exit.json records non-zero code=%d reason=%q, and memory.events oom_kill never increased; not an OOM kill",
				inst.Exit.Code, inst.Exit.Reason)},
		}}
	}
}
