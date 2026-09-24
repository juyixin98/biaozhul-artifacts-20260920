package service

import "time"

// Per-rule persisted state. These structs are JSON-encoded into the
// device_state table; the zero value is a valid fresh state.

// staleState tracks the stale rule, which is driven by the SERVER clock
// (ReceiveTime). Heartbeats count as proof of life.
type staleState struct {
	Open            bool       `json:"open"`
	Since           *time.Time `json:"since,omitempty"`            // when it entered stale
	HealthyDeadline *time.Time `json:"healthy_deadline,omitempty"` // when hysteresis recovery completes
	ContinuousSince *time.Time `json:"continuous_since,omitempty"` // start of current healthy streak
}

// frozenState tracks a run of equal-valued samples with advancing sequences.
type frozenState struct {
	Open        bool       `json:"open"`
	Value       string     `json:"value"`
	RunStartSeq int64      `json:"run_start_seq"`
	RunLastSeq  int64      `json:"run_last_seq"`
	RunStartAt  *time.Time `json:"run_start_at,omitempty"` // SampleTime of first sample in run
	RunLastAt   *time.Time `json:"run_last_at,omitempty"`  // SampleTime of most recent sample in run
	RunCount    int        `json:"run_count"`
	// Recovery run: number of consecutive samples whose value differs from
	// the frozen value while an alert is open.
	RecoverCount int        `json:"recover_count"`
	RecStartSeq  int64      `json:"rec_start_seq"`
	RecEndSeq    int64      `json:"rec_end_seq"`
	RecStartAt   *time.Time `json:"rec_start_at,omitempty"`
	RecEndAt     *time.Time `json:"rec_end_at,omitempty"`
}

// gapState tracks a sequence hole inside one epoch. FrontierSeq is the
// contiguous prefix high-watermark (every seq from the epoch's first sample up
// to FrontierSeq exists); PeakSeq is the highest seq observed. The open hole is
// exactly (FrontierSeq, PeakSeq]. A forward jump grows the hole; an in-order or
// backfilled sample that extends the contiguous prefix advances FrontierSeq and
// shrinks it, recovering the alert when it gets small enough.
type gapState struct {
	Open bool `json:"open"`

	FrontierSeq int64 `json:"frontier_seq"`
	PeakSeq     int64 `json:"peak_seq"`
	// BaseSeq is the first (lowest) seq observed in the epoch.
	BaseSeq int64 `json:"base_seq"`
	HasBase bool  `json:"has_base"`
	// Trigger snapshot at alert-open time.
	TrigStartSeq int64      `json:"trig_start_seq,omitempty"`
	TrigEndSeq   int64      `json:"trig_end_seq,omitempty"`
	TrigFirstAt  *time.Time `json:"trig_first_at,omitempty"`
	TrigLastAt   *time.Time `json:"trig_last_at,omitempty"`
	// Recovery range bookkeeping (seqs that filled the hole).
	RecStartSeq int64      `json:"rec_start_seq,omitempty"`
	RecEndSeq   int64      `json:"rec_end_seq,omitempty"`
	RecFirstAt  *time.Time `json:"rec_first_at,omitempty"`
	RecEndAt    *time.Time `json:"rec_end_at,omitempty"`
}
