package sim

import (
	"testing"

	"raftlab/internal/raft"
)

func baseCfg() ClusterConfig {
	return ClusterConfig{
		Size: 3, ElectionMin: 8, ElectionMax: 15, Heartbeat: 4, Latency: 1, Seed: 5,
	}
}

func TestElectLeaderAndCommit(t *testing.T) {
	cl, err := NewCluster(baseCfg())
	if err != nil {
		t.Fatal(err)
	}
	cl.Run(30)
	id, term, ok := cl.Leader()
	if !ok {
		t.Fatal("no leader elected")
	}
	if !cl.Propose(id, "SET hello world") {
		t.Fatal("propose rejected")
	}
	cl.Run(10)
	for _, nid := range cl.IDs() {
		s := cl.Node(nid)
		if s.CommitIndex < 1 {
			t.Fatalf("node %d commitIndex=%d", nid, s.CommitIndex)
		}
		if v, ok := cl.KV(nid)["hello"]; !ok || v != "world" {
			t.Fatalf("node %d kv=%v", nid, cl.KV(nid))
		}
	}
	t.Logf("leader n%d term %d committed the proposal", id, term)
}

func TestPartitionQuarantinesAndReleaseStale(t *testing.T) {
	cl, _ := NewCluster(baseCfg())
	cl.Run(25)

	// Isolate node 1; messages to it must accumulate in the stale queue.
	cl.Partition(1)
	cl.Run(5)
	if cl.StaleCount() == 0 {
		t.Fatal("expected quarantined messages during partition")
	}
	healedCount := cl.StaleCount()

	cl.Heal()
	n := cl.ReleaseStale()
	if n != healedCount {
		t.Fatalf("released %d stale messages, want %d", n, healedCount)
	}
	cl.Run(20)

	// Cluster must still have exactly one leader per term and all nodes
	// converge on the same committed log.
	leaders := cl.LeadersByTerm()
	for term, ls := range leaders {
		if len(ls) > 1 {
			t.Fatalf("term %d has multiple leaders %v", term, ls)
		}
	}
	var ref []raft.Entry
	for _, nid := range cl.IDs() {
		s := cl.Node(nid)
		if ref == nil {
			ref = s.Committed
			continue
		}
		if len(s.Committed) != len(ref) {
			t.Fatalf("node %d committed %d entries, want %d", nid, len(s.Committed), len(ref))
		}
		for i := range ref {
			if s.Committed[i] != ref[i] {
				t.Fatalf("node %d committed %+v at %d, want %+v", nid, s.Committed[i], i, ref[i])
			}
		}
	}
}

func TestStopStartPreservesCommittedLog(t *testing.T) {
	cl, _ := NewCluster(baseCfg())
	cl.Run(25)
	id, _, _ := cl.Leader()
	cl.Propose(id, "SET persist me")
	cl.Run(8)

	before := cl.Node(id).Committed
	cl.Stop(id)
	cl.Run(3)
	if err := cl.Start(id); err != nil {
		t.Fatal(err)
	}
	cl.Run(20)
	after := cl.Node(id).Committed
	if len(after) < len(before) {
		t.Fatalf("committed log shrank after restart: %d -> %d", len(before), len(after))
	}
	for i, e := range before {
		if after[i] != e {
			t.Fatalf("entry %d changed after restart: %+v vs %+v", i, e, after[i])
		}
	}
	// FSM replayed after reboot.
	if v, ok := cl.KV(id)["persist"]; !ok || v != "me" {
		t.Fatalf("FSM not restored: %q,%v", v, ok)
	}
}

func TestDeterministicReplay(t *testing.T) {
	run := func() []raft.Snapshot {
		cl, _ := NewCluster(baseCfg())
		cl.Run(25)
		id, _, _ := cl.Leader()
		cl.Propose(id, "NOOP")
		cl.Partition(id)
		cl.Run(10)
		cl.Heal()
		cl.Stop(id)
		cl.Run(2)
		if err := cl.Start(id); err != nil {
			t.Fatal(err)
		}
		cl.ReleaseStale()
		cl.Run(15)
		return cl.Nodes()
	}
	a, b := run(), run()
	for i := range a {
		if a[i].Term != b[i].Term || a[i].CommitIndex != b[i].CommitIndex ||
			a[i].Role != b[i].Role || len(a[i].Log) != len(b[i].Log) {
			t.Fatalf("node %d nondeterministic: %+v vs %+v", a[i].ID, a[i], b[i])
		}
	}
}
