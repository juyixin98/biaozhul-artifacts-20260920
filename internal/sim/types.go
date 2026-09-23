// Package sim is a deterministic single-process discrete-event simulator for a
// consistent-hash key/value store that grows and shrinks its cluster while
// serving concurrent, versioned writes.
//
// All randomness derives from the scenario seed; simulated time is a logical
// millisecond clock with no wall-clock or goroutine inside the simulation, so
// re-running the same scenario produces byte-identical results.
package sim

import "chsim/internal/ring"

// Record is one versioned value stored on a node.
type Record struct {
	Value   string `json:"value"`
	Version uint64 `json:"version"`
}

type kv struct {
	Key string `json:"key"`
	Rec Record `json:"record"`
}

// NetConfig configures the simulated network between the router and nodes.
type NetConfig struct {
	BaseDelayMs int64   `json:"base_delay_ms"` // fixed per-hop delay
	JitterMs    int64   `json:"jitter_ms"`     // uniform 0..Jitter added per copy -> reordering
	DropRate    float64 `json:"drop_rate"`     // probability a message is silently dropped
	DupRate     float64 `json:"dup_rate"`      // probability an extra duplicate is delivered
}

// TransferConfig tunes background range migration.
type TransferConfig struct {
	BatchSize      int   `json:"batch_size"`       // records per transfer batch
	Concurrency    int   `json:"concurrency"`      // ranges migrated in parallel
	FetchTimeoutMs int64 `json:"fetch_timeout_ms"` // resend timeout for migration messages
}

// ClientConfig generates a deterministic mixed workload. Op j (0-based) targets
// key "key-%04d" with index j modulo Keys. Every ReadEvery-th op is a read of
// the previous key; the rest are writes.
type ClientConfig struct {
	ID         string `json:"id"`
	StartMs    int64  `json:"start_ms"`
	Ops        int    `json:"ops"`
	IntervalMs int64  `json:"interval_ms"`
	Keys       int    `json:"keys"`       // shared keyspace key-0000..key-(Keys-1)
	ReadEvery  int    `json:"read_every"` // >0: one read every ReadEvery ops
}

// Op is a time-triggered control operation in the scenario.
type Op struct {
	T      int64  `json:"t"`
	Op     string `json:"op"` // add_node | remove_node | pause_migration | resume_migration
	NodeID string `json:"node_id,omitempty"`
	Weight int    `json:"weight,omitempty"`
}

// Scenario is the full JSON input for a simulation run.
type Scenario struct {
	Seed        int64          `json:"seed"`
	Vnodes      int            `json:"vnodes"` // vnodes per unit weight
	Nodes       []ring.Node    `json:"nodes"`  // initial membership
	Network     NetConfig      `json:"network"`
	Transfer    TransferConfig `json:"transfer"`
	Clients     []ClientConfig `json:"clients"`
	Ops         []Op           `json:"ops"`
	OpTimeoutMs int64          `json:"op_timeout_ms"` // client request resend timeout
	MaxAttempts int            `json:"max_attempts"`  // give-up threshold per request
	MaxEvents   int            `json:"max_events"`    // event-loop safety bound
}

// BarrierInfo records one cut-over barrier.
type BarrierInfo struct {
	Epoch         uint64 `json:"epoch"`         // epoch active after the barrier
	TimeMs        int64  `json:"time_ms"`       // logical time of the cut-over
	Tasks         int    `json:"tasks"`         // range transfers in this migration
	KeysMigrated  int    `json:"keys_migrated"` // records shipped (incl. retransmits)
	BytesMigrated int    `json:"bytes_migrated"`
}

// Stats summarizes what happened during the run.
type Stats struct {
	WritesIssued    int `json:"writes_issued"`
	WritesConfirmed int `json:"writes_confirmed"`
	WritesFailed    int `json:"writes_failed"`
	ReadsIssued     int `json:"reads_issued"`
	ReadsCompleted  int `json:"reads_completed"`
	ReadsFailed     int `json:"reads_failed"`
	StaleReads      int `json:"stale_reads"`

	MessagesSent       int `json:"messages_sent"`
	MessagesDelivered  int `json:"messages_delivered"`
	MessagesDropped    int `json:"messages_dropped"`
	MessagesDuplicated int `json:"messages_duplicated"`
	Retries            int `json:"retries"`

	KeysMigrated        int `json:"keys_migrated"` // records shipped across all migrations
	UniqueKeysMigrated  int `json:"unique_keys_migrated"`
	BytesMigrated       int `json:"bytes_migrated"`
	NodesDecommissioned int `json:"nodes_decommissioned"`

	Barriers []BarrierInfo `json:"barriers"`
}

// KeyIssue flags a key whose final owner does not hold the expected version.
type KeyIssue struct {
	Key         string `json:"key"`
	FinalOwner  string `json:"final_owner"`
	WantVersion uint64 `json:"want_version"`
	GotVersion  uint64 `json:"got_version"` // 0 if the owner holds no copy
	Detail      string `json:"detail"`
}

// ReadIssue flags a read that returned data older than a write already
// confirmed when the read was issued.
type ReadIssue struct {
	TimeMs     int64  `json:"time_ms"`
	Key        string `json:"key"`
	MinVersion uint64 `json:"min_version"`
	GotVersion uint64 `json:"got_version"`
	Found      bool   `json:"found"`
}

// RemovalIssue flags that a node was about to be decommissioned while holding
// the newest copy of a key nowhere else (the property the task forbids).
type RemovalIssue struct {
	Node      string `json:"node"`
	Key       string `json:"key"`
	Version   uint64 `json:"version"`
	Holder    string `json:"holder"`
	HolderVer uint64 `json:"holder_version"`
}

// Verification is the final acceptance check.
type Verification struct {
	Pass              bool           `json:"pass"`
	KeysChecked       int            `json:"keys_checked"`
	KeyIssues         []KeyIssue     `json:"key_issues"`
	StaleReads        []ReadIssue    `json:"stale_reads"`
	RemovalViolations []RemovalIssue `json:"removal_violations"`
	FailedOps         int            `json:"failed_ops"`
}

// Result is the JSON output of a run.
type Result struct {
	Stats        Stats        `json:"stats"`
	Verification Verification `json:"verification"`
	FinalNodes   []string     `json:"final_nodes"`
	FinalEpoch   uint64       `json:"final_epoch"`
	EndTimeMs    int64        `json:"end_time_ms"`
	Errors       []string     `json:"errors,omitempty"`
}
