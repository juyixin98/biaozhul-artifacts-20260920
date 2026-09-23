package raft

import (
	"testing"
)

func ms(n int64) int64 { return n * 1_000_000 }

// electLeader runs a fresh cluster long enough for one election and returns
// the simulator and the leader id.
func electLeader(t *testing.T, ids []int, seed int64) (*Simulator, int) {
	t.Helper()
	s := NewSimulator(ids, StandardVariant(), seed)
	s.Advance(ms(250), nil)
	l := s.Leader()
	if l < 0 {
		t.Fatalf("no leader elected after 250ms; views=%+v", s.Views())
	}
	return s, l
}

func TestElectionSingleLeaderPerTerm(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	term := s.Node(leader).Term()
	count := 0
	for _, id := range s.IDs() {
		n := s.Node(id)
		if n.Role() == Leader {
			count++
			if n.Term() != term {
				t.Fatalf("leader term mismatch: %d vs %d", n.Term(), term)
			}
		}
	}
	if count != 1 {
		t.Fatalf("want exactly 1 leader, got %d", count)
	}
	// Everybody knows at least the term.
	for _, id := range s.IDs() {
		if s.Node(id).Term() < term {
			t.Fatalf("node %d term %d below leader term %d", id, s.Node(id).Term(), term)
		}
	}
}

func TestElectionDeterministicAcrossSeeds(t *testing.T) {
	// Same seed => identical leader and term; different seed may differ but
	// still elects exactly one leader.
	s1, l1 := electLeader(t, []int{1, 2, 3}, 99)
	s2, l2 := electLeader(t, []int{1, 2, 3}, 99)
	if l1 != l2 || s1.Node(l1).Term() != s2.Node(l2).Term() {
		t.Fatalf("nondeterministic election: %d/term%d vs %d/term%d",
			l1, s1.Node(l1).Term(), l2, s2.Node(l2).Term())
	}
	s3, l3 := electLeader(t, []int{1, 2, 3}, 7)
	if l3 < 0 {
		t.Fatal("seed 7 produced no leader")
	}
	if s3.Node(l3).Role() != Leader {
		t.Fatal("seed 7 leader role wrong")
	}
}

func TestLogReplicationAndQuorumCommit(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	if !s.ClientCommand(leader, "SET k v1") {
		t.Fatal("ClientCommand rejected on leader")
	}
	s.Advance(ms(60), nil)

	// Leader committed the proposal (the election no-op occupies index 1).
	lc := s.Node(leader).CommitIndex()
	if lc != 2 {
		t.Fatalf("leader commitIndex = %d, want 2 (no-op + SET)", lc)
	}
	same := 0
	for _, id := range s.IDs() {
		n := s.Node(id)
		if n.CommitIndex() >= 2 {
			e := n.logSnapshotCommitted()
			if e[1].Command != "SET k v1" {
				t.Fatalf("node %d committed %v", id, e)
			}
			same++
		}
	}
	if same < 2 {
		t.Fatalf("want >=2 nodes with committed entry, got %d: %+v", same, s.Views())
	}
	// KV state machine applied.
	if got := s.Node(leader).KV()["k"]; got != "v1" {
		t.Fatalf("KV k = %q, want v1", got)
	}
}

// logSnapshotCommitted is a test helper returning committed entries.
func (n *Node) logSnapshotCommitted() []LogEntry {
	var out []LogEntry
	for i := 1; i <= n.commitIdx && i < len(n.log); i++ {
		out = append(out, n.log[i])
	}
	return out
}

func TestProposeToFollowerRejected(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	for _, id := range s.IDs() {
		if id != leader {
			if s.ClientCommand(id, "SET x y") {
				t.Fatalf("follower %d accepted proposal", id)
			}
		}
	}
}

func TestConflictRollback(t *testing.T) {
	// Node 4 joins as a "lagging" peer with a divergent old-term suffix:
	// append entries from the current leader must force truncation.
	s, leader := electLeader(t, []int{1, 2, 3, 4, 5}, 99)
	if !s.ClientCommand(leader, "SET a 1") {
		t.Fatal("propose 1 failed")
	}
	s.Advance(ms(60), nil)
	if !s.ClientCommand(leader, "SET a 2") {
		t.Fatal("propose 2 failed")
	}
	s.Advance(ms(60), nil)

	// Restart node 4 with a garbage divergent suffix at higher indexes in an
	// old term, simulating an unreplicated branch.
	laggard := s.Node(4)
	oldTerm := laggard.Term()
	for i := laggard.LastIndex() + 1; i <= 3; i++ {
		laggard.log = append(laggard.log, LogEntry{Term: oldTerm - 1, Index: i, Command: "BOGUS"})
	}
	s.Advance(ms(300), nil) // heartbeats should detect and repair the conflict

	got := laggard.logSnapshotCommitted()
	if len(got) < 2 {
		t.Fatalf("laggard failed to catch up: %+v", laggard.log)
	}
	for _, e := range got {
		if e.Command == "BOGUS" {
			t.Fatalf("conflicting suffix not truncated: %+v", laggard.log)
		}
	}
}

func TestRestartPerservesTermVoteLog(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	s.ClientCommand(leader, "SET p q")
	s.Advance(ms(60), nil)
	term := s.Node(leader).Term()
	lastIdx := s.Node(leader).LastIndex()

	s.Restart(leader)
	n := s.Node(leader)
	if n.Term() != term {
		t.Fatalf("term lost across restart: %d vs %d", n.Term(), term)
	}
	if n.LastIndex() != lastIdx {
		t.Fatalf("log lost across restart: lastIndex %d vs %d", n.LastIndex(), lastIdx)
	}
	if n.Role() != Follower {
		t.Fatalf("restarted node should be follower, got %s", n.Role())
	}
	// Cluster keeps serving and the restarted node catches up.
	s.Advance(ms(300), nil)
	newLeader := s.Leader()
	if newLeader < 0 {
		t.Fatal("no leader after restart")
	}
	s.ClientCommand(newLeader, "SET p r")
	s.Advance(ms(60), nil)
	if c := n.CommitIndex(); c < 2 {
		t.Fatalf("restarted node did not catch up, commitIndex=%d views=%+v", c, s.Views())
	}
}

func TestPersistenceVoteNotReusedAcrossTerms(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	term := s.Node(leader).Term()
	follower := -1
	for _, id := range s.IDs() {
		if id != leader {
			follower = id
		}
	}
	// Vote is persisted; restart then force a new term election, vote may be
	// granted once more only in the higher term.
	s.Restart(follower)
	if got := s.Node(follower).Term(); got != term {
		t.Fatalf("restarted follower term = %d, want %d", got, term)
	}
	// Partition the leader so a new election is needed.
	s.Network().Partition([][]int{{leader}, {follower, otherID(s.IDs(), leader, follower)}})
	s.Advance(ms(400), nil)
	s.Network().Heal()
	s.Advance(ms(300), nil)
	maxTerm := term
	for _, id := range s.IDs() {
		if t0 := s.Node(id).Term(); t0 > maxTerm {
			maxTerm = t0
		}
	}
	if maxTerm <= term {
		t.Fatalf("expected a new term after leader partition, still %d", term)
	}
	if s.Leader() < 0 {
		t.Fatal("no leader after heal")
	}
}

func otherID(ids []int, a, b int) int {
	for _, id := range ids {
		if id != a && id != b {
			return id
		}
	}
	return -1
}

func TestStaleLowerTermRPCRejected(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	followerTerm := s.Node(2).Term()
	// Forge a stale AppendEntries from a lower-term dead leader.
	stale := AppendEntries{
		Term: followerTerm - 1, LeaderID: 99, PrevLogI: 0, PrevLogT: 0,
		Entries: []LogEntry{{Term: followerTerm - 1, Index: 1, Command: "EVIL"}},
	}
	reply := s.Node(2).HandleAppendEntries(stale, s.Now(), ms(4))
	if reply == nil || reply.Append.Success {
		t.Fatalf("stale AE accepted: %+v", reply)
	}
	if s.Node(2).LastIndex() != 1 || s.Node(2).log[1].Command != noopCommand {
		t.Fatalf("stale AE mutated log: %+v", s.Node(2).log)
	}
	_ = leader
}

func TestPartitionMinorityCannotCommit(t *testing.T) {
	s, leader := electLeader(t, []int{1, 2, 3}, 99)
	s.ClientCommand(leader, "SET k 1")
	s.Advance(ms(40), nil)

	// Isolate the leader alone.
	others := []int{}
	for _, id := range s.IDs() {
		if id != leader {
			others = append(others, id)
		}
	}
	s.Network().Partition([][]int{{leader}, others})
	s.Advance(ms(400), nil)

	// Old leader may append but cannot commit a new entry without quorum.
	s.ClientCommand(leader, "SET k isolated")
	s.Advance(ms(200), nil)
	before := s.Node(leader).CommitIndex()
	s.ClientCommand(leader, "SET k isolated2")
	s.Advance(ms(200), nil)
	if s.Node(leader).CommitIndex() > before {
		t.Fatalf("isolated leader committed without majority: %d -> %d",
			before, s.Node(leader).CommitIndex())
	}
}
