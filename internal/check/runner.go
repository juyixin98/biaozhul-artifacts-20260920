// Package check enumerates short fault traces, runs them against the
// deterministic Raft simulator, and verifies the Raft safety properties.
// When an invariant is violated it reports a replayable, shrinkable
// counterexample trace.
package check

import (
	"fmt"

	"raftlab/internal/raft"
	"raftlab/internal/sim"
)

// Action is one step of a fault trace. Traces are plain data and serialize to
// JSON, so any counterexample can be stored and replayed bit-for-bit.
type Action struct {
	Op      string `json:"op"`
	Node    int    `json:"node,omitempty"`
	Group   string `json:"group,omitempty"`
	Command string `json:"command,omitempty"`
	Note    string `json:"note,omitempty"`
}

// Action kinds.
const (
	ActTick          = "tick"          // advance one virtual tick
	ActRun           = "run"           // advance Node ticks
	ActStop          = "stop"          // power a node off
	ActStart         = "start"         // reboot a node (reload from storage)
	ActRestart       = "restart"       // stop followed by start in one action
	ActPartition     = "partition"     // isolate a node into its own group
	ActSetGroup      = "setGroup"      // assign node to a partition group
	ActHeal          = "heal"          // restore full connectivity
	ActPropose       = "propose"       // submit command through a node
	ActProposeLeader = "proposeLeader" // submit through the current leader
	ActReleaseStale  = "releaseStale"  // deliver all delayed/old messages now
	ActDropStale     = "dropStale"     // discard all delayed messages
)

// Violation describes one failed invariant check.
type Violation struct {
	Kind     string               `json:"kind"`
	Tick     int                  `json:"tick"`
	Step     int                  `json:"step"`
	Message  string               `json:"message"`
	Leaders  map[int][]int        `json:"leadersByTerm,omitempty"`
	Index    int                  `json:"index,omitempty"`
	Prefixes map[int][]raft.Entry `json:"prefixes,omitempty"`
}

// TraceResult is the outcome of running one trace.
type TraceResult struct {
	Violation *Violation `json:"violation,omitempty"`
	FinalTick int        `json:"finalTick"`
	Leader    int        `json:"leader"`
}

func (t TraceResult) OK() bool { return t.Violation == nil }

// Runner executes traces against fresh deterministic clusters.
type Runner struct {
	cfg sim.ClusterConfig
}

func NewRunner(cfg sim.ClusterConfig) *Runner { return &Runner{cfg: cfg} }

// Config returns the runner's cluster configuration.
func (r *Runner) Config() sim.ClusterConfig { return r.cfg }

// Run executes a trace, checking invariants after every tick.
func (r *Runner) Run(actions []Action) (*sim.Cluster, TraceResult) {
	cl, err := sim.NewCluster(r.cfg)
	if err != nil {
		panic(err)
	}
	res := TraceResult{Leader: -1}

	for i, a := range actions {
		Apply(cl, a)
		if a.Op == ActTick {
			res.FinalTick = cl.Tick()
			if v := CheckInvariants(cl, i); v != nil {
				res.Violation = v
				return cl, res
			}
		}
	}
	res.FinalTick = cl.Tick()
	res.Leader = cl.LeaderID()
	if v := CheckInvariants(cl, len(actions)); v != nil {
		res.Violation = v
	}
	return cl, res
}

// Apply performs one action on a cluster. Exported for scenario builders that
// construct a trace interactively against a live cluster.
func Apply(cl *sim.Cluster, a Action) {
	switch a.Op {
	case ActTick:
		cl.Advance()
	case ActRun:
		cl.Run(a.Node)
	case ActStop:
		cl.Stop(a.Node)
	case ActStart:
		if err := cl.Start(a.Node); err != nil {
			panic(err)
		}
	case ActRestart:
		cl.Stop(a.Node)
		if err := cl.Start(a.Node); err != nil {
			panic(err)
		}
	case ActPartition:
		cl.Partition(a.Node)
	case ActSetGroup:
		cl.SetGroup(a.Node, a.Group)
	case ActHeal:
		cl.Heal()
	case ActPropose:
		cl.Propose(a.Node, a.Command)
	case ActProposeLeader:
		cl.ProposeLeader(a.Command)
	case ActReleaseStale:
		cl.ReleaseStale()
	case ActDropStale:
		cl.DropStale()
	default:
		panic(fmt.Sprintf("unknown action %q", a.Op))
	}
}
