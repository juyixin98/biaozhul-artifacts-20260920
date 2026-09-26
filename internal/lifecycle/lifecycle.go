// Package lifecycle defines the four-phase graceful shutdown state model and
// the structured result report produced by every shutdown run.
package lifecycle

import (
	"encoding/json"
	"time"
)

// Phase is one discrete state of the shutdown state machine.
type Phase string

const (
	// PhaseRunning: server accepts new work normally.
	PhaseRunning Phase = "RUNNING"
	// PhaseStopAccept: phase 1 — traffic cut. Listeners close, readiness flips.
	PhaseStopAccept Phase = "STOP_ACCEPT"
	// PhaseDraining: phase 2 — accepted in-flight work is allowed to finish.
	PhaseDraining Phase = "DRAINING"
	// PhaseCancelling: phase 3 — drain budget spent, remaining work is cancelled.
	PhaseCancelling Phase = "CANCELLING"
	// PhaseClosing: phase 4 — resources shut down in reverse registration order.
	PhaseClosing Phase = "CLOSING"
	// PhaseClosed: terminal state.
	PhaseClosed Phase = "CLOSED"
)

// OrderedPhases is the exact required transition order.
var OrderedPhases = []Phase{
	PhaseStopAccept,
	PhaseDraining,
	PhaseCancelling,
	PhaseClosing,
	PhaseClosed,
}

// Outcome is the definitive result of an accepted unit of work.
type Outcome string

const (
	OutcomeCompleted Outcome = "completed"
	OutcomeCancelled Outcome = "cancelled"
	OutcomeRejected  Outcome = "rejected"
)

// Event is one entry on the shutdown timeline.
type Event struct {
	Phase Phase     `json:"phase"`
	At    time.Time `json:"at"`
	Note  string    `json:"note,omitempty"`
}

// RequestRecord is the structured per-work result.
type RequestRecord struct {
	ID         string    `json:"id"`
	Kind       string    `json:"kind"`
	Outcome    Outcome   `json:"outcome"`
	StartedAt  time.Time `json:"startedAt"`
	FinishedAt time.Time `json:"finishedAt"`
	Detail     string    `json:"detail,omitempty"`
}

// CloseRecord documents one resource closing.
type CloseRecord struct {
	Name       string    `json:"name"`
	Order      int       `json:"order"`
	At         time.Time `json:"at"`
	Active     int64     `json:"activeTasksAtClose"`
	Err        string    `json:"error,omitempty"`
	DurationMS int64     `json:"durationMs"`
}

// Report is the structured result of one shutdown run.
type Report struct {
	StartedAt  time.Time `json:"startedAt"`
	FinishedAt time.Time `json:"finishedAt"`
	DurationMS int64     `json:"durationMs"`
	Signals    int       `json:"signalsReceived"`
	Timeline   []Event   `json:"timeline"`
	Completed  int       `json:"completed"`
	Cancelled  int       `json:"cancelled"`
	Rejected   int       `json:"rejected"`
	// BackgroundSpawned counts accepted background jobs; AfterStop counts jobs
	// that were (incorrectly) spawned after STOP_ACCEPT — must always be 0.
	BackgroundSpawned        int             `json:"backgroundSpawned"`
	BackgroundAfterStop      int             `json:"backgroundSpawnedAfterStopAccept"`
	RejectedBackgroundSpawns int             `json:"rejectedBackgroundSpawns"`
	InFlightAtClose          int             `json:"inFlightAtClose"`
	CloseOrder               []CloseRecord   `json:"closeOrder"`
	Requests                 []RequestRecord `json:"requests"`
}

// JSON renders the report as indented JSON.
func (r Report) JSON() ([]byte, error) {
	return json.MarshalIndent(r, "", "  ")
}
