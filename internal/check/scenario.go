package check

import (
	"fmt"

	"raftlab/internal/raft"
	"raftlab/internal/sim"
)

// Figure8Config is the deterministic 5-node configuration used by the
// hand-built Figure 8 scenario. Nodes 3 and 4 have very long timeouts so they
// never start an election on their own; the scenario controls every term via
// nodes 1 and 5 (timeouts 10 and 12 ticks).
func Figure8Config(buggy bool) sim.ClusterConfig {
	return sim.ClusterConfig{
		Size:        5,
		ElectionMin: 10,
		ElectionMax: 11, // fixed map overrides this per node
		Heartbeat:   5,
		Latency:     1,
		Seed:        1,
		FixedTimeout: map[int]int{
			1: 10,
			2: 50,
			3: 10000,
			4: 10000,
			5: 12,
		},
		BuggyOldTermCommit: buggy,
	}
}

// scenarioBuilder drives one cluster and records the concrete actions taken so
// the resulting trace can be replayed independently from JSON.
type scenarioBuilder struct {
	actions []Action
	cl      *sim.Cluster
}

func (b *scenarioBuilder) do(a Action) {
	Apply(b.cl, a)
	b.actions = append(b.actions, a)
}

func (b *scenarioBuilder) tick() { b.do(Action{Op: ActTick}) }

func (b *scenarioBuilder) run(n int) {
	for i := 0; i < n; i++ {
		b.tick()
	}
}

func (b *scenarioBuilder) wait(cond func() bool, limit int, what string) error {
	for i := 0; i < limit; i++ {
		if cond() {
			return nil
		}
		b.tick()
	}
	return fmt.Errorf("%s: condition not met within %d ticks", what, limit)
}

func (b *scenarioBuilder) waitLeader(id, term, limit int) error {
	return b.wait(func() bool {
		lid, t, ok := b.cl.Leader()
		return ok && lid == id && t == term
	}, limit, fmt.Sprintf("waitLeader(n%d,t%d)", id, term))
}

func (b *scenarioBuilder) group(id int, g string) {
	b.do(Action{Op: ActSetGroup, Node: id, Group: g})
}

func (b *scenarioBuilder) stop(id int)  { b.do(Action{Op: ActStop, Node: id}) }
func (b *scenarioBuilder) start(id int) { b.do(Action{Op: ActStart, Node: id}) }

func (b *scenarioBuilder) propose(id int, cmd string) {
	b.do(Action{Op: ActPropose, Node: id, Command: cmd})
}

// Figure8Actions builds the Figure 8 fault scenario from the Raft paper:
//
//	(a) term1 leader n1: entry "x" reaches only n2, then n1 is powered off.
//	(b) n5 wins term 2 in {3,4,5}, writes entry "y" only to n3, then stops.
//	(c) n1 is re-elected (term 3) from {1,2,4} and replicates the old-term
//	    entry "x" to a majority, but cannot commit it; the buggy variant does.
//	(d) n5 returns, wins term 4 from {2,3,4,5}, overwrites x with y.
//	(e) n5 commits a term-4 entry, which implicitly commits y at index 1.
//
// With BuggyOldTermCommit the final state commits two different entries at
// index 1 on different nodes; with correct Raft no invariant is violated.
// The returned trace records every concrete tick and is directly replayable
// via Runner.Run or the HTTP /v1/replay endpoint.
func Figure8Actions(buggy bool) ([]Action, error) {
	cfg := Figure8Config(buggy)
	cl, err := sim.NewCluster(cfg)
	if err != nil {
		return nil, err
	}
	b := &scenarioBuilder{cl: cl, actions: []Action{}}

	// (a) wait for n1 to be elected leader of term 1.
	if err := b.waitLeader(1, 1, 40); err != nil {
		return nil, err
	}
	// Propose x, then split the network and power n1 off: only the AE to n2
	// lands; AEs to {3,4,5} and n2's ack are quarantined as stale.
	b.propose(1, "SET k x")
	b.group(1, "L")
	b.group(2, "L")
	b.group(3, "R")
	b.group(4, "R")
	b.group(5, "R")
	b.stop(1)
	b.run(2)

	// (b) n5 wins term 2 in {3,4,5}.
	if err := b.waitLeader(5, 2, 40); err != nil {
		return nil, err
	}
	// Isolate n4 so y is written to n3 only; stop n5 after the AE lands but
	// before n3's ack is processed, leaving y uncommitted.
	b.group(4, "X")
	b.propose(5, "SET k y")
	b.tick()
	b.stop(5)
	b.run(2) // n3's ack to n5 is quarantined

	// (c) Reconnect {1,2,4}, keep n3 isolated, restart n1. n4 already voted
	// for n5 in term 2, so n1 first loses a term-2 campaign and wins term 3.
	b.group(1, "L")
	b.group(2, "L")
	b.group(4, "L")
	b.group(3, "Z")
	b.start(1)
	if err := b.waitLeader(1, 3, 60); err != nil {
		return nil, err
	}
	// n1 replicates the old-term entry x onto n4 (majority {1,2,4}). The
	// buggy variant commits x here; correct Raft keeps commitIndex at 0.
	if buggy {
		if err := b.wait(func() bool {
			s := b.cl.Node(1)
			return s.CommitIndex >= 1
		}, 12, "buggy n1 committing old-term x"); err != nil {
			return nil, err
		}
	} else {
		// Give replication time to converge; commitIndex must remain 0.
		b.run(6)
	}

	// (d) n1 powered off; n5 returns with {2,3,4,5}. n5's log is up-to-date,
	// it wins term 4 and overwrites x with y.
	b.stop(1)
	b.group(1, "P1")
	b.group(2, "A")
	b.group(3, "A")
	b.group(4, "A")
	b.group(5, "A")
	b.start(5)
	if err := b.waitLeader(5, 4, 60); err != nil {
		return nil, err
	}

	// (e) n5 commits a current-term entry; index 1 (y) commits implicitly.
	// In the buggy run index 1 was already committed as x on {1,2,4} ->
	// committed-prefix conflict.
	b.propose(5, "SET k z")
	if err := b.wait(func() bool {
		return b.cl.Node(5).CommitIndex >= 2
	}, 30, "n5 committing through index 2"); err != nil {
		return nil, err
	}

	// Demonstrate old-message tolerance: all packets delayed by the
	// partitions are delivered after n1 rejoins; stale terms are ignored.
	b.group(1, "A")
	b.start(1)
	b.run(3)
	b.do(Action{Op: ActReleaseStale, Note: "deliver all messages delayed by partitions"})
	b.run(4)

	return b.actions, nil
}

// entryAt returns the committed entry at 1-based index idx.
func entryAt(s raft.Snapshot, idx int) (raft.Entry, bool) {
	if idx < 1 || idx > len(s.Committed) {
		return raft.Entry{}, false
	}
	return s.Committed[idx-1], true
}
