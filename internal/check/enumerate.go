package check

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"hash"

	"raftlab/internal/sim"
)

// EnumerationConfig bounds the exhaustive search over short fault traces.
type EnumerationConfig struct {
	Cluster      sim.ClusterConfig // base cluster config (size/seed/timeouts/bug)
	MaxDepth     int               // maximum fault-action depth per trace
	MaxTraces    int               // hard cap on executed traces
	TicksPerStep int               // ticks inserted between fault actions
	Commands     []string          // commands proposed during traces
	// Seeds are trace prefixes explored before the blind DFS. They guarantee
	// coverage of windows random depth-limited walks almost never hit,
	// notably the Figure 8 uncommitted-entry interleaving.
	Seeds [][]Action
}

// EnumerationReport summarizes a search.
type EnumerationReport struct {
	TracesRun  int               `json:"tracesRun"`
	MaxDepth   int               `json:"maxDepth"`
	StatesSeen int               `json:"statesSeen"`
	Violations []*Counterexample `json:"violations,omitempty"`
	Exhausted  bool              `json:"exhausted"` // false if MaxTraces was hit
}

// Counterexample is a violating trace, serialized for replay, with a shrunk
// minimal form.
type Counterexample struct {
	Violation  *Violation `json:"violation"`
	Trace      []Action   `json:"trace"`
	TraceJSON  string     `json:"traceJson"`
	Shrunk     []Action   `json:"shrunk,omitempty"`
	ShrunkJSON string     `json:"shrunkJson,omitempty"`
}

func buildCounterexample(v *Violation, trace []Action, cc sim.ClusterConfig) *Counterexample {
	ce := &Counterexample{Violation: v, Trace: trace}
	b, _ := json.Marshal(trace)
	ce.TraceJSON = string(b)
	shrunk := Shrink(trace, v.Kind, cc)
	if len(shrunk) > 0 && len(shrunk) < len(trace) {
		ce.Shrunk = shrunk
		sb, _ := json.Marshal(shrunk)
		ce.ShrunkJSON = string(sb)
	}
	return ce
}

// Enumerate performs a bounded depth-first search over fault traces. At each
// level it considers: stopping/starting/restarting each node, isolating a
// node, healing the network, proposals (through every node and through the
// current leader), and stale-message release. Traces reaching the same hashed
// cluster state are merged, which keeps execution counts small even though the
// raw tree is exponential.
func Enumerate(cfg EnumerationConfig) EnumerationReport {
	if cfg.MaxDepth <= 0 {
		cfg.MaxDepth = 3
	}
	if cfg.MaxTraces <= 0 {
		cfg.MaxTraces = 3000
	}
	if cfg.TicksPerStep <= 0 {
		cfg.TicksPerStep = 3
	}
	if len(cfg.Commands) == 0 {
		cfg.Commands = []string{"NOOP", "SET k v"}
	}

	r := NewRunner(cfg.Cluster)
	report := EnumerationReport{MaxDepth: cfg.MaxDepth, Exhausted: true}
	seen := map[string]bool{}

	var dfs func(prefix []Action, depth int)
	dfs = func(prefix []Action, depth int) {
		for _, a := range candidateActions(r, prefix, cfg) {
			if report.TracesRun >= cfg.MaxTraces {
				report.Exhausted = false
				return
			}
			trace := append(append([]Action(nil), prefix...), a)
			report.TracesRun++
			cl, res := r.Run(trace)
			if !res.OK() {
				report.Violations = append(report.Violations,
					buildCounterexample(res.Violation, trace, cfg.Cluster))
				continue
			}
			h := stateHash(cl)
			if seen[h] {
				continue
			}
			seen[h] = true
			report.StatesSeen = len(seen)
			if depth < cfg.MaxDepth {
				extended := append(append([]Action(nil), trace...),
					Action{Op: ActRun, Node: cfg.TicksPerStep})
				dfs(extended, depth+1)
			}
		}
	}

	// Deterministic seeds (e.g. the Figure 8 scenario) are executed first.
	for _, seed := range cfg.Seeds {
		if report.TracesRun >= cfg.MaxTraces {
			report.Exhausted = false
			break
		}
		report.TracesRun++
		cl, res := r.Run(seed)
		if !res.OK() {
			report.Violations = append(report.Violations,
				buildCounterexample(res.Violation, seed, cfg.Cluster))
		}
		seen[stateHash(cl)] = true
		report.StatesSeen = len(seen)
	}

	dfs([]Action{{Op: ActRun, Node: cfg.TicksPerStep}}, 0)
	return report
}

// candidateActions generates the fault actions available at a search node.
func candidateActions(r *Runner, prefix []Action, cfg EnumerationConfig) []Action {
	cl, _ := r.Run(prefix)
	size := len(cl.IDs())
	var actions []Action

	for id := 1; id <= size; id++ {
		s := cl.Node(id)
		if s.Alive {
			actions = append(actions,
				Action{Op: ActStop, Node: id},
				Action{Op: ActRestart, Node: id},
				Action{Op: ActPartition, Node: id},
			)
		} else {
			actions = append(actions, Action{Op: ActStart, Node: id})
		}
		for _, cmd := range cfg.Commands {
			actions = append(actions, Action{Op: ActPropose, Node: id, Command: cmd})
		}
	}
	actions = append(actions, Action{Op: ActHeal})
	actions = append(actions, Action{Op: ActReleaseStale})
	for _, cmd := range cfg.Commands {
		actions = append(actions, Action{Op: ActProposeLeader, Command: cmd})
	}
	return actions
}

// stateHash hashes every node snapshot for dedup. Deterministic and
// allocation-light; connectivity is reflected indirectly through node state.
func stateHash(cl *sim.Cluster) string {
	h := sha256.New()
	for _, s := range cl.Nodes() {
		h.Write([]byte(s.Role))
		h.Write([]byte{0})
		writeInt(h, s.ID)
		writeInt(h, s.Term)
		writeInt(h, s.VotedFor)
		writeInt(h, s.LeaderID)
		writeInt(h, s.CommitIndex)
		writeInt(h, s.LastApplied)
		if s.Alive {
			h.Write([]byte{1})
		} else {
			h.Write([]byte{0})
		}
		for _, e := range s.Log {
			writeInt(h, e.Term)
			h.Write([]byte(e.Command))
			h.Write([]byte{0})
		}
		h.Write([]byte{'|'})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func writeInt(h hash.Hash, v int) {
	h.Write([]byte{
		byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v),
	})
}

// Default3NodeConfig is the config used by the CLI/HTTP enumeration by
// default: 3 nodes, random (but seeded) timeouts, in-memory storage.
func Default3NodeConfig() sim.ClusterConfig {
	return sim.ClusterConfig{
		Size:        3,
		ElectionMin: 8,
		ElectionMax: 15,
		Heartbeat:   4,
		Latency:     1,
		Seed:        42,
	}
}
