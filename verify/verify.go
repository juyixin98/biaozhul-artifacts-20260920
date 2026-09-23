// Package verify enumerates short fault traces against the simulated Raft
// cluster and asserts Raft's core safety invariants, with deterministic
// counterexample capture and replay.
package verify

import (
	"fmt"
	"strings"

	"raftlab/raft"
)

// Action is one fault-injection / workload step in a scenario.
type Action struct {
	Kind      string `json:"kind"` // see Kind* constants
	Node      int    `json:"node,omitempty"`
	Peers     []int  `json:"peers,omitempty"`
	Command   string `json:"command,omitempty"`
	AdvanceMs int64  `json:"advanceMs,omitempty"`
}

// Action kinds.
const (
	KindAdvance     = "advance"
	KindPartition   = "partition"
	KindHeal        = "heal"
	KindPause       = "pause"
	KindResume      = "resume"
	KindPauseFrom   = "pause-from"
	KindResumeFrom  = "resume-from"
	KindRestart     = "restart"
	KindPropose     = "propose"
	KindProposeAll  = "propose-all"
	KindStaleAppend = "stale-append" // directly inject an old leader's RPC
)

func (a Action) String() string {
	switch a.Kind {
	case KindAdvance:
		return fmt.Sprintf("advance(%dms)", a.AdvanceMs)
	case KindPartition:
		return fmt.Sprintf("partition%v", a.Peers)
	case KindHeal:
		return "heal"
	case KindPause:
		return fmt.Sprintf("pause(%d)", a.Node)
	case KindResume:
		return fmt.Sprintf("resume(%d)", a.Node)
	case KindPauseFrom:
		return fmt.Sprintf("pauseFrom(%d)", a.Node)
	case KindResumeFrom:
		return fmt.Sprintf("resumeFrom(%d)", a.Node)
	case KindRestart:
		return fmt.Sprintf("restart(%d)", a.Node)
	case KindPropose:
		return fmt.Sprintf("propose(%d,%q)", a.Node, a.Command)
	case KindProposeAll:
		return fmt.Sprintf("proposeAll(%q)", a.Command)
	case KindStaleAppend:
		return fmt.Sprintf("staleAppend(%d->%d,%q)", a.Peers[0], a.Node, a.Command)
	default:
		return a.Kind
	}
}

// Scenario is a deterministic, replayable fault trace.
type Scenario struct {
	Name    string   `json:"name"`
	Nodes   int      `json:"nodes"`
	Variant string   `json:"variant"` // "standard" | "naive"
	Seed    int64    `json:"seed"`
	Actions []Action `json:"actions"`
}

func (sc Scenario) String() string {
	parts := make([]string, len(sc.Actions))
	for i, a := range sc.Actions {
		parts[i] = a.String()
	}
	return sc.Name + "[" + strings.Join(parts, " -> ") + "]"
}

// Violation describes one failed invariant.
type Violation struct {
	Invariant string `json:"invariant"`
	Detail    string `json:"detail"`
	Step      int    `json:"step"`
	At        int64  `json:"at"`
}

func (v Violation) Error() string {
	return fmt.Sprintf("%s at step %d (t=%dns): %s", v.Invariant, v.Step, v.At, v.Detail)
}

// Checker observes a simulator and accumulates the first violation of each
// invariant. It reads node state directly through pointers — no allocations —
// so it is cheap enough to run after every event during enumeration.
type Checker struct {
	// immutableAt[node][index] records the (term,command) first observed as
	// committed; a later different value means the committed prefix changed.
	immutable  map[int]map[int]observedEntry
	leaders    map[int]int // term -> node id
	violations []Violation
}

type observedEntry struct {
	term    int
	command string
}

func NewChecker() *Checker {
	return &Checker{
		immutable: map[int]map[int]observedEntry{},
		leaders:   map[int]int{},
	}
}

func (c *Checker) Violations() []Violation { return c.violations }

// Observe runs all invariant checks against the current simulator state.
// step counts scenario actions, t is virtual time (ns).
func (c *Checker) Observe(s *raft.Simulator, step int, t int64) {
	c.checkLeaderUniqueness(s, step, t)
	c.checkCommittedPrefix(s, step, t)
}

func (c *Checker) checkLeaderUniqueness(s *raft.Simulator, step int, t int64) {
	for _, id := range s.IDs() {
		if !s.Alive(id) {
			continue
		}
		n := s.Node(id)
		if n.Role() != raft.Leader {
			continue
		}
		term := n.Term()
		if prev, ok := c.leaders[term]; ok && prev != id {
			c.add(Violation{
				Invariant: "LeaderUniquenessPerTerm",
				Detail:    fmt.Sprintf("term %d had leaders %d and %d", term, prev, id),
				Step:      step, At: t,
			})
		} else {
			c.leaders[term] = id
		}
	}
}

func (c *Checker) checkCommittedPrefix(s *raft.Simulator, step int, t int64) {
	// snapshot of this round's committed entries per node for cross-node check
	round := map[int][]raft.LogEntry{}
	for _, id := range s.IDs() {
		if !s.Alive(id) {
			continue
		}
		entries := s.CommittedPrefix(id)
		round[id] = entries

		seen := c.immutable[id]
		if seen == nil {
			seen = map[int]observedEntry{}
			c.immutable[id] = seen
		}
		for _, e := range entries {
			cur := observedEntry{term: e.Term, command: e.Command}
			if old, ok := seen[e.Index]; ok {
				if old != cur {
					c.add(Violation{
						Invariant: "CommittedPrefixImmutable",
						Detail: fmt.Sprintf("node %d index %d changed from term=%d cmd=%q to term=%d cmd=%q",
							id, e.Index, old.term, old.command, cur.term, cur.command),
						Step: step, At: t,
					})
				}
			} else {
				seen[e.Index] = cur
			}
		}
	}

	// Cross-node: committed prefixes must never conflict at the same index.
	// Compare every pair at every index they have both committed; commands
	// must be identical (terms may legitimately differ only if entries match).
	var ids []int
	for id := range round {
		ids = append(ids, id)
	}
	for i := 0; i < len(ids); i++ {
		for j := i + 1; j < len(ids); j++ {
			a, b := round[ids[i]], round[ids[j]]
			m := len(a)
			if len(b) < m {
				m = len(b)
			}
			for k := 0; k < m; k++ {
				if a[k].Index != b[k].Index || a[k].Term != b[k].Term || a[k].Command != b[k].Command {
					c.add(Violation{
						Invariant: "CommittedPrefixAgreement",
						Detail: fmt.Sprintf("nodes %d and %d disagree at index %d: term=%d cmd=%q vs term=%d cmd=%q",
							ids[i], ids[j], k+1, a[k].Term, a[k].Command, b[k].Term, b[k].Command),
						Step: step, At: t,
					})
				}
			}
		}
	}
}

func (c *Checker) add(v Violation) {
	for _, ex := range c.violations {
		if ex.Invariant == v.Invariant {
			return // keep only the first of each kind
		}
	}
	c.violations = append(c.violations, v)
}

// Result summarizes a scenario run.
type Result struct {
	Scenario   Scenario          `json:"scenario"`
	Violations []Violation       `json:"violations"`
	Leader     int               `json:"leader"`
	Views      []raft.NodeView   `json:"views,omitempty"`
	Trace      []raft.TraceEvent `json:"trace,omitempty"`
	OK         bool              `json:"ok"`
}

// Options controls a run.
type Options struct {
	Trace        bool
	CaptureViews bool
	// EpilogueMs runs extra virtual time after the final action so pending
	// messages settle before the last observations.
	EpilogueMs int64
}

// Run executes sc from an empty cluster and returns the invariant verdict.
func Run(sc Scenario, opts Options) Result {
	variant := raft.StandardVariant()
	switch sc.Variant {
	case "naive":
		variant = raft.NaiveVariant()
	case "notermcheck":
		variant = raft.StaleAppendVariant()
	}
	s := raft.NewSimulator(nodeIDs(sc.Nodes), variant, sc.Seed)
	if opts.Trace {
		s.EnableTrace()
	}
	chk := NewChecker()

	observe := func(step int) {
		chk.Observe(s, step, s.Now())
	}
	observe(0)

	advance := func(ms int64, step int) {
		if ms <= 0 {
			return
		}
		s.Advance(ms*1_000_000, func(*raft.Simulator) { observe(step) })
		observe(step)
	}

	for i, a := range sc.Actions {
		step := i + 1
		switch a.Kind {
		case KindAdvance:
			advance(a.AdvanceMs, step)
		case KindPartition:
			partition(s, a.Peers)
			observe(step)
		case KindHeal:
			s.Network().Heal()
			observe(step)
		case KindPause:
			s.Pause(a.Node)
			observe(step)
		case KindResume:
			s.Resume(a.Node)
			observe(step)
		case KindPauseFrom:
			s.PauseFrom(a.Node)
			observe(step)
		case KindResumeFrom:
			s.ResumeFrom(a.Node)
			observe(step)
		case KindRestart:
			s.Restart(a.Node)
			observe(step)
		case KindPropose:
			s.ClientCommand(a.Node, a.Command)
			observe(step)
		case KindProposeAll:
			// Client command routed to the current leader (the highest-term
			// live leader); non-leaders reject it.
			if id := currentLeader(s); id >= 0 {
				s.ClientCommand(id, a.Command)
			}
			observe(step)
		case KindStaleAppend:
			// Forge an old leader's AppendEntries carrying a foreign entry at
			// index 1: this is exactly the "delayed old message" a real network
			// could deliver. Standard Raft rejects it via the term check; the
			// naive variant truncates the committed prefix.
			s.InjectMessage(raft.Message{
				Kind: raft.MsgAppendReq,
				From: a.Peers[0], To: a.Node, Term: 1,
				Append: &raft.AppendEntries{
					Term:     1,
					LeaderID: a.Peers[0],
					PrevLogI: 0,
					PrevLogT: 0,
					Entries: []raft.LogEntry{
						{Term: 1, Index: 1, Command: a.Command},
					},
					Commit: 1,
				},
			})
			observe(step)
		}
	}

	if opts.EpilogueMs > 0 {
		advance(opts.EpilogueMs, len(sc.Actions)+1)
	}

	res := Result{
		Scenario:   sc,
		Violations: chk.Violations(),
		Leader:     s.Leader(),
		OK:         len(chk.Violations()) == 0,
	}
	if opts.CaptureViews {
		res.Views = s.Views()
	}
	if opts.Trace {
		res.Trace = s.Trace
	}
	return res
}

func nodeIDs(n int) []int {
	ids := make([]int, n)
	for i := range ids {
		ids[i] = i + 1
	}
	return ids
}

// currentLeader returns the live leader with the highest term, or -1.
func currentLeader(s *raft.Simulator) int {
	best, bestTerm := -1, -1
	for _, id := range s.IDs() {
		if !s.Alive(id) {
			continue
		}
		n := s.Node(id)
		if n.Role() == raft.Leader && n.Term() > bestTerm {
			best, bestTerm = id, n.Term()
		}
	}
	return best
}

// partition installs a fault: isolated is one node cut from the rest; the
// remaining nodes stay fully connected.
func partition(s *raft.Simulator, isolated []int) {
	set := map[int]bool{}
	for _, id := range isolated {
		set[id] = true
	}
	var rest []int
	for _, id := range s.IDs() {
		if !set[id] {
			rest = append(rest, id)
		}
	}
	groups := [][]int{append([]int(nil), isolated...)}
	if len(rest) > 0 {
		groups = append(groups, rest)
	}
	s.Network().Partition(groups)
}
