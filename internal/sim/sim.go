package sim

import (
	"container/heap"
	"errors"
	"fmt"
	"math/rand"
	"sort"

	"chsim/internal/ring"
)

// Sim runs one scenario. It is single-threaded: the event loop is the only
// place that mutates node stores, the router and the result.
type Sim struct {
	cfg    Scenario
	now    int64
	seq    uint64
	events eventHeap
	rng    *rand.Rand

	nodes   map[string]*Node
	removed map[string]*Node
	router  *Router

	result *Result

	// observed truth built from client responses
	confirmed    map[string]Record // highest version clients got an ack for
	migratedKeys map[string]bool

	staleIssues   []ReadIssue
	removalIssues []RemovalIssue
	eventCount    int
}

// Validate fills defaults and checks the scenario.
func Validate(sc *Scenario) error {
	if len(sc.Nodes) == 0 {
		return errors.New("scenario needs at least one initial node")
	}
	seen := map[string]bool{}
	for _, n := range sc.Nodes {
		if n.ID == "" {
			return errors.New("node id must not be empty")
		}
		if seen[n.ID] {
			return fmt.Errorf("duplicate initial node %q", n.ID)
		}
		seen[n.ID] = true
	}
	if sc.Vnodes <= 0 {
		sc.Vnodes = 64
	}
	if sc.Network.BaseDelayMs < 0 {
		return errors.New("network.base_delay_ms must be >= 0")
	}
	if sc.Network.JitterMs < 0 {
		return errors.New("network.jitter_ms must be >= 0")
	}
	if sc.Network.DropRate < 0 || sc.Network.DropRate >= 1 {
		return errors.New("network.drop_rate must be in [0,1)")
	}
	if sc.Network.DupRate < 0 || sc.Network.DupRate >= 1 {
		return errors.New("network.dup_rate must be in [0,1)")
	}
	if sc.Transfer.BatchSize <= 0 {
		sc.Transfer.BatchSize = 32
	}
	if sc.Transfer.Concurrency <= 0 {
		sc.Transfer.Concurrency = 4
	}
	if sc.Transfer.FetchTimeoutMs <= 0 {
		sc.Transfer.FetchTimeoutMs = 100
	}
	if sc.OpTimeoutMs <= 0 {
		sc.OpTimeoutMs = 200
	}
	if sc.MaxAttempts <= 0 {
		sc.MaxAttempts = 30
	}
	if sc.MaxEvents <= 0 {
		sc.MaxEvents = 2_000_000
	}
	ids := map[string]bool{}
	for i, c := range sc.Clients {
		if c.ID == "" {
			return fmt.Errorf("clients[%d].id must not be empty", i)
		}
		if c.Keys <= 0 || c.Ops < 0 || c.IntervalMs < 0 {
			return fmt.Errorf("clients[%d] (%s): keys must be > 0 and ops/interval >= 0", i, c.ID)
		}
		if ids[c.ID] {
			return fmt.Errorf("duplicate client id %q", c.ID)
		}
		ids[c.ID] = true
	}
	valid := map[string]bool{"add_node": true, "remove_node": true,
		"pause_migration": true, "resume_migration": true}
	for i, op := range sc.Ops {
		if !valid[op.Op] {
			return fmt.Errorf("ops[%d]: unknown op %q", i, op.Op)
		}
		if (op.Op == "add_node" || op.Op == "remove_node") && op.NodeID == "" {
			return fmt.Errorf("ops[%d]: %s needs node_id", i, op.Op)
		}
	}
	return nil
}

// Run validates and executes a scenario, returning the stats and the final
// acceptance verification. It never panics on scenario input errors.
func Run(sc Scenario) (*Result, error) {
	if err := Validate(&sc); err != nil {
		return nil, err
	}
	s := &Sim{
		cfg:          sc,
		rng:          rand.New(rand.NewSource(sc.Seed)),
		nodes:        make(map[string]*Node),
		removed:      make(map[string]*Node),
		result:       &Result{},
		confirmed:    make(map[string]Record),
		migratedKeys: make(map[string]bool),
	}
	s.router = newRouter(s)
	s.result.Stats.Barriers = []BarrierInfo{}

	for _, n := range sc.Nodes {
		w := n.Weight
		if w <= 0 {
			w = 1
		}
		s.router.members[n.ID] = w
		s.nodes[n.ID] = newNode(n.ID)
	}
	s.router.ring = ring.Build(sc.Nodes, sc.Vnodes)

	s.scheduleWorkload()
	ops := make([]Op, len(sc.Ops))
	copy(ops, sc.Ops)
	sort.SliceStable(ops, func(i, j int) bool { return ops[i].T < ops[j].T })
	for _, op := range ops {
		op := op
		s.schedule(op.T, func() { s.router.control(op) })
	}

	for s.events.Len() > 0 {
		if s.eventCount >= sc.MaxEvents {
			s.addError(fmt.Sprintf("event budget %d exhausted", sc.MaxEvents))
			break
		}
		s.eventCount++
		e := heap.Pop(&s.events).(event)
		s.now = e.t
		e.fn()
	}

	s.verify()
	s.result.EndTimeMs = s.now
	s.result.FinalEpoch = s.router.epoch
	s.result.FinalNodes = s.router.ring.NodeIDs()
	return s.result, nil
}

func (s *Sim) addError(msg string) {
	s.result.Errors = append(s.result.Errors, msg)
}

// ---------------------------------------------------------------------------
// Truth tracking, driven by router callbacks
// ---------------------------------------------------------------------------

func (s *Sim) onWriteConfirmed(key string, rec Record) {
	if cur, ok := s.confirmed[key]; !ok || rec.Version > cur.Version {
		s.confirmed[key] = rec
	}
}

func (s *Sim) maxConfirmedVersion(key string) uint64 {
	return s.confirmed[key].Version
}

// onReadResult applies the staleness rule: a read must not return data older
// than a version that was already confirmed when the read was issued.
func (s *Sim) onReadResult(key string, rec Record, found bool, minConf uint64) {
	violates := false
	if found {
		violates = rec.Version < minConf
	} else {
		violates = minConf > 0
	}
	if violates {
		s.result.Stats.StaleReads++
		s.staleIssues = append(s.staleIssues, ReadIssue{
			TimeMs: s.now, Key: key, MinVersion: minConf, GotVersion: rec.Version, Found: found,
		})
	}
}
