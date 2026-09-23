// Package sim implements a deterministic discrete-event simulator for the
// weighted quorum protocol. Everything runs in one process: "nodes" are
// stateless responders, the network is an injectable policy, and time is a
// virtual integer clock. No goroutines, sockets or real clusters are used.
//
// The network can independently drop, delay, reorder and duplicate
// request/response messages. A fixed seed makes every run bit-for-bit
// reproducible.
package sim

// SimNode mirrors quorum.Node for the run interface.
type SimNode struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
	Domain string `json:"domain,omitempty"`
}

// Rule is one scripted network rule. The first rule (in order) whose fields
// match a transmission decides what happens to it.
type Rule struct {
	From       string `json:"from,omitempty"`        // sender node ID, or "client"
	To         string `json:"to,omitempty"`          // receiver node ID, or "client"
	Role       string `json:"role,omitempty"`        // "A" | "B" | "dead" | "" (any)
	MsgType    string `json:"msg_type,omitempty"`    // "request" | "response" | "" (any)
	Op         string `json:"op,omitempty"`          // "read" | "write" | "" (any)
	MinAttempt int    `json:"min_attempt,omitempty"` // match attempt >= this (0 = any)
	Action     string `json:"action"`                // "deliver" | "drop" | "duplicate"
	Delay      int    `json:"delay,omitempty"`       // virtual time units when delivered
	Duplicates int    `json:"duplicates,omitempty"`  // extra copies for "duplicate"
}

// Network configures the stochastic network policy.
type Network struct {
	LossRate      float64 `json:"loss_rate,omitempty"`      // [0,1], per transmission
	DuplicateRate float64 `json:"duplicate_rate,omitempty"` // [0,1], per transmission
	MinDelay      int     `json:"min_delay,omitempty"`      // inclusive
	MaxDelay      int     `json:"max_delay,omitempty"`      // inclusive
	ReorderWindow int     `json:"reorder_window,omitempty"` // >0 enables reordering
}

// Sim is the simulator section of a run request.
type Sim struct {
	Seed          uint64   `json:"seed,omitempty"`
	Runs          int      `json:"runs,omitempty"`
	Horizon       int      `json:"horizon,omitempty"`
	ClientTimeout int      `json:"client_timeout,omitempty"`
	MaxAttempts   int      `json:"max_attempts,omitempty"`
	Operations    int      `json:"operations,omitempty"`
	Kind          string   `json:"kind,omitempty"` // "ww", "rw", "mixed" (default)
	FailedDomains []string `json:"failed_domains,omitempty"`
	Network       *Network `json:"network,omitempty"`
	Rules         []Rule   `json:"rules,omitempty"`
}

// RunInput is one simulator invocation.
type RunInput struct {
	Nodes       []SimNode `json:"nodes"`
	ReadQuorum  int       `json:"read_quorum"`
	WriteQuorum int       `json:"write_quorum"`
	Sim         Sim       `json:"sim"`
}

// Violation is one observed intersection violation between two completed ops.
type Violation struct {
	OpA         string   `json:"op_a"`
	OpB         string   `json:"op_b"`
	Kind        string   `json:"kind"`
	RespondersA []string `json:"responders_a"`
	RespondersB []string `json:"responders_b"`
}

// TraceEvent is one simulator event in execution order.
type TraceEvent struct {
	T      int    `json:"t"`
	Type   string `json:"type"`
	Detail string `json:"detail,omitempty"`
}

// RunResult is the result of one (seeded) simulation run.
type RunResult struct {
	Seed               uint64       `json:"seed"`
	Completed          int          `json:"completed"`
	TimedOut           int          `json:"timed_out"`
	RequestsSent       int          `json:"requests_sent"`
	ResponsesDelivered int          `json:"responses_delivered"`
	Dropped            int          `json:"dropped"`
	Duplicated         int          `json:"duplicated"`
	MaxTime            int          `json:"max_time"`
	Violations         []Violation  `json:"violations,omitempty"`
	CompletedOps       []OpSummary  `json:"completed_ops,omitempty"`
	Trace              []TraceEvent `json:"trace,omitempty"`
}

// OpSummary records one completed operation and its responder set.
type OpSummary struct {
	Op          string   `json:"op"`
	Kind        string   `json:"kind"`
	Responders  []string `json:"responders"`
	Weight      int      `json:"weight"`
	CompletedAt int      `json:"completed_at"`
}

// Report is the aggregate result over all runs.
type Report struct {
	Runs           int         `json:"runs"`
	Scripted       bool        `json:"scripted"`
	CompletedTotal int         `json:"completed_total"`
	TimedOutTotal  int         `json:"timed_out_total"`
	ViolationRuns  int         `json:"violation_runs"`
	Violations     []Violation `json:"violations,omitempty"`
	RunResults     []RunResult `json:"run_results,omitempty"`
	AllComplete    bool        `json:"all_complete"`
	Safe           bool        `json:"safe"` // no completed op pair intersects-empty
	Errors         []string    `json:"errors,omitempty"`
}
