package raft

import (
	"math/rand"
	"testing"
)

func testConfig(n int) Config {
	ids := make([]int, n)
	for i := range ids {
		ids[i] = i + 1
	}
	return Config{
		Nodes:       ids,
		ElectionMin: 8,
		ElectionMax: 15,
		Heartbeat:   4,
	}
}

func TestElectionSingleLeaderAndMajority(t *testing.T) {
	cfg := testConfig(5)
	nodes, _, fsms := buildTestCluster(t, cfg, 42)
	cl := newTestHarness(nodes, fsms)

	cl.run(40)

	leaders := map[int][]int{}
	for _, n := range nodes {
		if n.Alive() && n.Role() == Leader {
			leaders[n.Term()] = append(leaders[n.Term()], n.id)
		}
	}
	if len(leaders) != 1 {
		t.Fatalf("want exactly 1 term with a leader, got %v", leaders)
	}
	for term, ls := range leaders {
		if len(ls) != 1 {
			t.Fatalf("term %d has %d leaders: %v", term, len(ls), ls)
		}
	}
}

func TestLeaderCommitMajority(t *testing.T) {
	cfg := testConfig(3)
	nodes, _, fsms := buildTestCluster(t, cfg, 7)
	cl := newTestHarness(nodes, fsms)
	cl.run(30)

	leader := cl.leader()
	if leader == nil {
		t.Fatal("no leader elected")
	}
	msgs, ok := leader.Propose("SET a 1")
	if !ok {
		t.Fatal("propose rejected by leader")
	}
	cl.send(msgs...)
	cl.run(10)

	for _, n := range nodes {
		if n.CommitIndex() < 1 {
			t.Fatalf("node %d commitIndex=%d, want >=1", n.id, n.CommitIndex())
		}
		if v, ok := cl.fsms[n.id].Get("a"); !ok || v != "1" {
			t.Fatalf("node %d applied state %q,%v", n.id, v, ok)
		}
	}
}

func TestNoCommitWithoutMajority(t *testing.T) {
	cfg := testConfig(3)
	nodes, _, fsms := buildTestCluster(t, cfg, 7)
	cl := newTestHarness(nodes, fsms)
	cl.run(30)
	leader := cl.leader()
	id := leader.id

	// Isolate the leader alone: it keeps accepting proposals locally
	// (uncommitted), but nothing reaches a majority.
	cl.partition(id)
	if msgs, ok := leader.Propose("SET b 2"); ok {
		cl.send(msgs...)
	}
	cl.run(10)

	if ci := leader.CommitIndex(); ci > 0 {
		// The partitioned leader may retain old commits from before, but the
		// new entry at index ci must not commit: check the last log entry.
		if e := leader.entry(leader.lastIndex()); e.Command == "SET b 2" &&
			leader.CommitIndex() >= leader.lastIndex() {
			t.Fatal("partitioned leader committed without a majority")
		}
	}
	// Exactly: the proposed entry must not be in any node's committed prefix.
	for _, n := range nodes {
		s := n.Snapshot()
		for _, e := range s.Committed {
			if e.Command == "SET b 2" {
				t.Fatalf("node %d committed SET b 2 without majority", n.id)
			}
		}
	}
}

func TestLogConflictTruncation(t *testing.T) {
	// Follower has [term1 a, term2 OLD], leader sends an AE with prevIndex=1
	// carrying [term3 NEW]: the conflicting term2 entry must be truncated.
	n := newSingle(t, 1, Config{Nodes: []int{1, 2}, ElectionMin: 8, ElectionMax: 9, Heartbeat: 4})
	n.role = Follower
	n.currentTerm = 3
	n.votedFor = -1
	n.log = []Entry{{Term: 1, Command: "a"}, {Term: 2, Command: "OLD"}}

	reply := n.Step(Message{
		Type: MsgAppendEntries, From: 2, To: 1, Term: 3,
		PrevLogIndex: 1, PrevLogTerm: 1,
		Entries:      []Entry{{Term: 3, Command: "NEW"}},
		LeaderCommit: 0,
	})
	if len(reply) != 1 || !reply[0].Grant {
		t.Fatalf("AE rejected: %+v", reply)
	}
	if n.lastIndex() != 2 || n.entry(2).Command != "NEW" || n.entry(2).Term != 3 {
		t.Fatalf("conflicting tail not replaced: %+v", n.log)
	}
	if n.entry(1).Command != "a" {
		t.Fatal("matching prefix entry was modified")
	}
}

func TestAppendEntriesBackoffHint(t *testing.T) {
	// Leader asks for prevIndex=5, follower only has 3 entries and reports a
	// term conflict; the next AE must backtrack instead of looping.
	n := newSingle(t, 2, Config{Nodes: []int{1, 2}, ElectionMin: 8, ElectionMax: 9, Heartbeat: 4})
	n.role = Follower
	n.currentTerm = 1
	n.log = []Entry{{Term: 1, Command: "x"}, {Term: 1, Command: "y"}, {Term: 2, Command: "z"}}
	reply := n.Step(Message{
		Type: MsgAppendEntries, From: 1, To: 2, Term: 1,
		PrevLogIndex: 5, PrevLogTerm: 2,
	})
	if len(reply) != 1 || reply[0].Grant {
		t.Fatalf("expected rejection, got %+v", reply)
	}
	if reply[0].HintIndex != n.lastIndex()+1 {
		t.Fatalf("missing-entry hint = %d, want %d", reply[0].HintIndex, n.lastIndex()+1)
	}
}

func TestPersistenceAcrossRestart(t *testing.T) {
	cfg := testConfig(3)
	nodes, storages, fsms := buildTestCluster(t, cfg, 7)
	cl := newTestHarness(nodes, fsms)
	cl.run(30)
	leader := cl.leader()
	id := leader.id
	msgs, _ := leader.Propose("SET persist yes")
	cl.send(msgs...)
	cl.run(10)

	wantCI := nodes[id-1].CommitIndex()
	wantLog := nodes[id-1].LogEntries()

	// Power off and reboot: same storage, fresh FSM, state replayed.
	nodes[id-1].Stop()
	newFSM := NewKVStateMachine()
	restarted, err := NewNode(id, cfg, rand.New(rand.NewSource(1)), storages[id], newFSM)
	if err != nil {
		t.Fatal(err)
	}
	if err := restarted.Restart(); err != nil {
		t.Fatal(err)
	}
	if restarted.Term() != nodes[id-1].Term() {
		t.Fatalf("term lost across restart: %d vs %d", restarted.Term(), nodes[id-1].Term())
	}
	if restarted.CommitIndex() != wantCI {
		t.Fatalf("commitIndex lost: %d vs %d", restarted.CommitIndex(), wantCI)
	}
	got := restarted.LogEntries()
	if len(got) != len(wantLog) {
		t.Fatalf("log length changed across restart: %d vs %d", len(got), len(wantLog))
	}
	for i := range got {
		if got[i] != wantLog[i] {
			t.Fatalf("log entry %d changed: %+v vs %+v", i, got[i], wantLog[i])
		}
	}
	// FSM was replayed from the persisted applied index.
	if v, ok := newFSM.Get("persist"); !ok || v != "yes" {
		t.Fatalf("FSM not replayed after restart: %q,%v", v, ok)
	}
}

func TestRejectStaleTermVote(t *testing.T) {
	n := newSingle(t, 1, Config{Nodes: []int{1, 2, 3}, ElectionMin: 8, ElectionMax: 9, Heartbeat: 4})
	n.currentTerm = 5
	n.votedFor = 2
	reply := n.Step(Message{
		Type: MsgRequestVote, From: 3, To: 1, Term: 4,
		LastLogIndex: 10, LastLogTerm: 4,
	})
	if reply[0].Grant || reply[0].Term != 5 {
		t.Fatalf("stale-term RV was granted: %+v", reply)
	}
}

func TestVoteDeniedForShorterLog(t *testing.T) {
	n := newSingle(t, 1, Config{Nodes: []int{1, 2, 3}, ElectionMin: 8, ElectionMax: 9, Heartbeat: 4})
	n.currentTerm = 3
	n.votedFor = -1
	n.log = []Entry{{Term: 1}, {Term: 3}, {Term: 3}}
	reply := n.Step(Message{
		Type: MsgRequestVote, From: 2, To: 1, Term: 3,
		LastLogIndex: 2, LastLogTerm: 3,
	})
	if reply[0].Grant {
		t.Fatal("granted vote to candidate with shorter log")
	}
}

func TestHigherTermStepsDownLeader(t *testing.T) {
	n := newSingle(t, 1, Config{Nodes: []int{1, 2}, ElectionMin: 8, ElectionMax: 9, Heartbeat: 4})
	n.role = Leader
	n.currentTerm = 3
	n.Step(Message{
		Type: MsgAppendEntries, From: 2, To: 1, Term: 4,
		PrevLogIndex: 0,
	})
	if n.Role() != Follower || n.Term() != 4 {
		t.Fatalf("leader did not step down: role=%s term=%d", n.Role(), n.Term())
	}
}
