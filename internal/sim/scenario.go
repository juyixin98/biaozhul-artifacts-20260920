package sim

// Scenario is the JSON run interface. One scenario file describes one
// deterministic simulation: configuration, the client roster, and a timed
// script of client actions.
type Scenario struct {
	Name  string `json:"name"`
	Seed  uint64 `json:"seed"`
	TTL   Time   `json:"ttl_ms"`
	EndAt Time   `json:"end_ms"`
	// Heartbeat is the default renew interval for clients; individual
	// clients may override it.
	Heartbeat Time `json:"heartbeat_ms"`
	// RetryGap is how long a client waits before retrying a "busy" acquire.
	RetryGap Time `json:"retry_gap_ms"`

	Network Network      `json:"network"`
	Clients []ClientSpec `json:"clients"`
	Actions []ActionSpec `json:"actions"`
}

// ClientSpec declares one client node.
type ClientSpec struct {
	ID        string `json:"id"`
	Heartbeat Time   `json:"heartbeat_ms,omitempty"`
}

// Action op values for the scenario script.
const (
	OpAcquire = "acquire"
	OpRenew   = "renew" // explicit one-off renew on top of automatic heartbeats
	OpSubmit  = "submit"
	OpRelease = "release"
	OpPause   = "pause"  // freeze the client for Duration
	OpResume  = "resume" // un-pause early (optional; pause also ends by duration)
	OpNote    = "note"   // record a labeled marker in the trace
)

// ActionSpec is one timed client action.
type ActionSpec struct {
	At       Time   `json:"at_ms"`
	Client   string `json:"client"`
	Op       string `json:"op"`
	Value    string `json:"value,omitempty"`
	Duration Time   `json:"duration_ms,omitempty"` // pause
	Resource string `json:"resource,omitempty"`    // defaults to "R"
	// Retry, for acquire: keep retrying busy responses until granted or the
	// client's lease is lost.
	Retry bool `json:"retry,omitempty"`
	// ForceSubmit submits even after the client believes its lease is gone.
	// This models an old holder that never observed the loss (e.g. a frozen
	// GC pause) and keeps writing; the resource fence is what stops it.
	ForceSubmit bool `json:"force_submit,omitempty"`
}
