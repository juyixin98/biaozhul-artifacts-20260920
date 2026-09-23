package check

import (
	"fmt"

	"raftlab/internal/raft"
	"raftlab/internal/sim"
)

// Violation kinds.
const (
	ViolationElectionSafety  = "election-safety"
	ViolationCommittedPrefix = "committed-prefix-conflict"
)

// CheckInvariants runs the safety properties against the current cluster
// state. It returns the first violation found (highest priority first).
//
// Properties checked (from the Raft paper, Figure 3):
//
//  1. Election Safety: at most one live node can believe it is leader for a
//     given term.
//  2. Log Matching / Leader Completeness on committed prefixes: if two nodes
//     have both committed an entry at the same index, the entries (term and
//     command) must be identical. Committed entries of different nodes must
//     form compatible prefixes of one shared log.
func CheckInvariants(cl *sim.Cluster, step int) *Violation {
	if v := checkElectionSafety(cl, step); v != nil {
		return v
	}
	if v := checkCommittedPrefix(cl, step); v != nil {
		return v
	}
	return nil
}

func checkElectionSafety(cl *sim.Cluster, step int) *Violation {
	byTerm := cl.LeadersByTerm()
	for term, leaders := range byTerm {
		if len(leaders) > 1 {
			return &Violation{
				Kind:    ViolationElectionSafety,
				Tick:    cl.Tick(),
				Step:    step,
				Leaders: byTerm,
				Message: fmt.Sprintf("term %d has %d live leaders at once: %v", term, len(leaders), leaders),
			}
		}
	}
	return nil
}

func checkCommittedPrefix(cl *sim.Cluster, step int) *Violation {
	history := cl.CommittedHistory()
	// canonical[term,index] style check: for each index, collect the entry of
	// every node that committed through it; they must all agree.
	idx := 1
	for {
		var canonical *raft.Entry
		var who []int
		prefixes := map[int][]raft.Entry{}
		any := false
		for id, committed := range history {
			if len(committed) >= idx {
				any = true
				e := committed[idx-1]
				if canonical == nil {
					ee := e
					canonical = &ee
					who = []int{id}
				} else if e.Term != canonical.Term || e.Command != canonical.Command {
					prefixes[id] = committed
				} else {
					who = append(who, id)
				}
			}
		}
		if !any {
			return nil
		}
		if len(prefixes) > 0 {
			// include the canonical holder's prefix for the report
			prefixes[who[0]] = history[who[0]]
			return &Violation{
				Kind:     ViolationCommittedPrefix,
				Tick:     cl.Tick(),
				Step:     step,
				Index:    idx,
				Prefixes: prefixes,
				Message: fmt.Sprintf(
					"committed entries conflict at index %d: node %d committed (t%d,%q) but other nodes committed different entries: %s",
					idx, who[0], canonical.Term, canonical.Command, describeConflict(idx, history)),
			}
		}
		idx++
	}
}

func describeConflict(idx int, history map[int][]raft.Entry) string {
	s := ""
	for id, committed := range history {
		if len(committed) >= idx {
			e := committed[idx-1]
			s += fmt.Sprintf("{node %d: term=%d cmd=%q} ", id, e.Term, e.Command)
		}
	}
	return s
}
