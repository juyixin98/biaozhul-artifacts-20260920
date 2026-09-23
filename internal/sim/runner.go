package sim

import (
	"encoding/json"
	"fmt"
)

// InvariantCheck is one automatically verified safety property.
type InvariantCheck struct {
	Name   string `json:"name"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail,omitempty"`
}

// Report is the JSON result of running one scenario.
type Report struct {
	Scenario      string               `json:"scenario"`
	Seed          uint64               `json:"seed"`
	EndTime       Time                 `json:"end_time_ms"`
	Events        int64                `json:"events_processed"`
	NetworkStats  Stats                `json:"network_stats"`
	Lock          LockSnapshot         `json:"lock"`
	Resource      ResourceSnapshot     `json:"resource"`
	Clients       []ClientEventSummary `json:"clients"`
	Invariants    []InvariantCheck     `json:"invariants"`
	AllInvariants bool                 `json:"all_invariants_pass"`
	Trace         []TraceEvent         `json:"trace"`
}

// Result bundles the report with the live objects for tests.
type Result struct {
	Report   *Report
	Lock     *LockService
	Resource *Resource
	Clients  map[string]*Client
}

// Validate checks a parsed scenario for self-consistency.
func (s *Scenario) Validate() error {
	if s.Name == "" {
		return fmt.Errorf("scenario.name is required")
	}
	if s.TTL <= 0 {
		return fmt.Errorf("scenario.ttl_ms must be positive")
	}
	if s.EndAt <= 0 {
		return fmt.Errorf("scenario.end_ms must be positive")
	}
	if s.Heartbeat <= 0 {
		return fmt.Errorf("scenario.heartbeat_ms must be positive")
	}
	if s.Heartbeat >= s.TTL {
		return fmt.Errorf("heartbeat_ms (%d) must be below ttl_ms (%d)", s.Heartbeat, s.TTL)
	}
	if s.RetryGap <= 0 {
		return fmt.Errorf("scenario.retry_gap_ms must be positive")
	}
	if s.Network.MaxLatency < s.Network.MinLatency || s.Network.MinLatency < 0 {
		return fmt.Errorf("invalid latency bounds")
	}
	seen := map[string]bool{}
	for _, c := range s.Clients {
		if c.ID == "" {
			return fmt.Errorf("client with empty id")
		}
		if c.ID == LockID || c.ID == ResourceID {
			return fmt.Errorf("client id %q is reserved", c.ID)
		}
		if seen[c.ID] {
			return fmt.Errorf("duplicate client id %q", c.ID)
		}
		seen[c.ID] = true
		hb := c.Heartbeat
		if hb == 0 {
			hb = s.Heartbeat
		}
		if hb >= s.TTL {
			return fmt.Errorf("client %q heartbeat %d >= ttl %d", c.ID, hb, s.TTL)
		}
	}
	validOps := map[string]bool{
		OpAcquire: true, OpRenew: true, OpSubmit: true, OpRelease: true,
		OpPause: true, OpResume: true, OpNote: true,
	}
	for i, a := range s.Actions {
		if !seen[a.Client] {
			return fmt.Errorf("actions[%d]: unknown client %q", i, a.Client)
		}
		if !validOps[a.Op] {
			return fmt.Errorf("actions[%d]: unknown op %q", i, a.Op)
		}
		if a.At > s.EndAt {
			return fmt.Errorf("actions[%d]: at_ms %d beyond end_ms %d", i, a.At, s.EndAt)
		}
		if a.Op == OpPause && a.Duration <= 0 {
			return fmt.Errorf("actions[%d]: pause requires duration_ms > 0", i)
		}
	}
	return nil
}

// Run parses/validates/executes a scenario and builds its report.
func Run(s *Scenario) (*Result, error) {
	if err := s.Validate(); err != nil {
		return nil, err
	}

	trace := NewTrace()
	net := &s.Network // share stats with the scenario object
	eng := NewEngine(net, trace, s.Seed, s.EndAt)

	lock := NewLockService(s.TTL)
	res := NewResource()
	eng.AddNode(lock)
	eng.AddNode(res)

	clients := map[string]*Client{}
	var clientOrder []string
	for _, cs := range s.Clients {
		hb := cs.Heartbeat
		if hb == 0 {
			hb = s.Heartbeat
		}
		c := NewClient(cs.ID, hb, s.RetryGap)
		clients[cs.ID] = c
		clientOrder = append(clientOrder, cs.ID)
		eng.AddNode(c)
	}

	actions := make([]*ActionSpec, len(s.Actions))
	for i := range s.Actions {
		actions[i] = &s.Actions[i]
	}
	processed, err := eng.Run(actions)
	if err != nil {
		return nil, err
	}

	rep := &Report{
		Scenario:     s.Name,
		Seed:         s.Seed,
		EndTime:      eng.Now(),
		Events:       processed,
		NetworkStats: net.Stats,
		Lock:         lock.Snapshot(),
		Resource:     res.Snapshot(),
		Clients:      []ClientEventSummary{},
		Trace:        []TraceEvent{},
	}
	for _, id := range clientOrder {
		rep.Clients = append(rep.Clients, clients[id].Snapshot())
	}
	if trace.Events != nil {
		rep.Trace = trace.Events
	}
	rep.Invariants = []InvariantCheck{}
	rep.Invariants = CheckInvariants(rep, trace)
	rep.AllInvariants = true
	for _, c := range rep.Invariants {
		if !c.Pass {
			rep.AllInvariants = false
			break
		}
	}

	return &Result{Report: rep, Lock: lock, Resource: res, Clients: clients}, nil
}

// RunJSON executes a scenario provided as JSON bytes.
func RunJSON(data []byte) (*Result, error) {
	s := &Scenario{}
	if err := json.Unmarshal(data, s); err != nil {
		return nil, fmt.Errorf("invalid scenario json: %w", err)
	}
	return Run(s)
}
