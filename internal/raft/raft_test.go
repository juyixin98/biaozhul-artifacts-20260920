package raft

import (
	"testing"
)

// memStore 是测试用内存持久化实现。
type memStore struct{ ps *PersistentState }

func newMemStore() *memStore { return &memStore{} }

func (m *memStore) Load() (*PersistentState, error) {
	if m.ps == nil {
		return nil, nil
	}
	cp := *m.ps
	cp.Log = append([]Entry(nil), m.ps.Log...)
	return &cp, nil
}

func (m *memStore) Save(ps PersistentState) error {
	cp := ps
	cp.Log = append([]Entry(nil), ps.Log...)
	m.ps = &cp
	return nil
}

func (m *memStore) Reset() error { m.ps = nil; return nil }

// testEnv 记录节点意图，便于单元测试断言。
type testEnv struct {
	now       int64
	msgs      []Message
	elections []int64
	hearts    []int64
}

func (e *testEnv) Now() int64                        { return e.now }
func (e *testEnv) Send(m Message)                    { e.msgs = append(e.msgs, m) }
func (e *testEnv) ScheduleElection(d int64, _ int64) { e.elections = append(e.elections, d) }
func (e *testEnv) ScheduleHeartbeat(d int64)         { e.hearts = append(e.hearts, d) }

func testCfg(id int) Config {
	return Config{
		ID: id, Peers: []int{1, 2, 3},
		HeartbeatPeriod: 50, ElectionMin: 150, ElectionMax: 300,
	}
}

func newTestNode(id int) (*Node, *testEnv, *memStore) {
	env := &testEnv{}
	st := newMemStore()
	n := NewNode(testCfg(id), env, st, newRand(1))
	return n, env, st
}

// TestElectionNeedsMajority 验证候选人只有自己一票时不会成为领导者，
// 收到一张反对票也不会成为领导者。
func TestElectionNeedsMajority(t *testing.T) {
	n1, env1, _ := newTestNode(1)
	n2, env2, _ := newTestNode(2)

	n1.ElectionTick(n1.electionNonce)
	if n1.role != Candidate {
		t.Fatalf("n1 should be candidate, got %v", n1.role)
	}
	// 节点2的日志比候选人更新（含高任期条目），因此拒绝投票。
	n2.Log = append(n2.Log, Entry{Term: 9, Command: "ahead"})
	var req Message
	for _, m := range env1.msgs {
		if m.Type == MsgVote && m.To == 2 {
			req = m
		}
	}
	n2.Step(req)
	var deny Message
	for _, m := range env2.msgs {
		if m.Type == MsgVoteResp && m.To == 1 {
			deny = m
		}
	}
	if deny.Grant {
		t.Fatalf("n2 with newer log must deny vote")
	}
	n1.Step(deny)
	if n1.role == Leader {
		t.Fatalf("candidate with only own vote must not become leader")
	}
}

// TestVoteAndWin 走完整投票流程：候选人收到一张赞成票即当选。
func TestVoteAndWin(t *testing.T) {
	n1, env1, _ := newTestNode(1)
	n2, env2, _ := newTestNode(2)
	n3, _, _ := newTestNode(3)
	_ = env2

	// 节点1以最新 nonce 触发选举。
	n1.ElectionTick(n1.electionNonce)
	if n1.role != Candidate || n1.CurrentTerm != 1 {
		t.Fatalf("n1 should be candidate term1, got role=%v term=%d", n1.role, n1.CurrentTerm)
	}
	var voteTo2, voteTo3 Message
	for _, m := range env1.msgs {
		if m.Type == MsgVote && m.To == 2 {
			voteTo2 = m
		}
		if m.Type == MsgVote && m.To == 3 {
			voteTo3 = m
		}
	}
	if voteTo2.To != 2 || voteTo3.To != 3 {
		t.Fatalf("candidate should request votes from 2 and 3: %+v", env1.msgs)
	}

	// 节点2赞成（空日志同样新）。
	n2.Step(voteTo2)
	var resp Message
	for _, m := range env2.msgs {
		if m.Type == MsgVoteResp && m.To == 1 && m.Grant {
			resp = m
		}
	}
	if resp.To != 1 {
		t.Fatalf("n2 should grant vote: %+v", env2.msgs)
	}
	n1.Step(resp)
	if n1.role != Leader {
		t.Fatalf("n1 should be leader with 2/3 votes, got %v", n1.role)
	}

	// 节点3迟到的赞成票送达时节点1已是领导者，不应产生异常。
	n3.Step(voteTo3)
}

// TestRejectStaleLeader 验证旧任期追加请求被拒绝且不推进提交。
func TestRejectStaleLeader(t *testing.T) {
	n1, _, _ := newTestNode(1)
	n2, _, _ := newTestNode(2)

	// 让节点2处于更高任期。
	n2.CurrentTerm = 5
	n2.VotedFor = 2
	n2.persist()

	// 旧领导者任期4发来追加。
	stale := Message{Type: MsgApp, From: 1, To: 2, Term: 4,
		PrevLogIdx: 0, PrevLogTerm: 0, LeaderCommit: 0,
		Entries: []Entry{{Term: 4, Command: "stale"}}}
	n2.Step(stale)
	snap := n2.Inspect()
	if len(snap.Log) != 1 { // 只有哨兵
		t.Fatalf("stale append must not modify log, got %d entries", len(snap.Log)-1)
	}
	if snap.CommitIndex != 0 {
		t.Fatalf("stale append must not advance commit, got %d", snap.CommitIndex)
	}
	_ = n1
}

// TestCurrentTermCommitRule 验证只提交当前任期条目（多数派确认）。
func TestCurrentTermCommitRule(t *testing.T) {
	n1, env1, _ := newTestNode(1)
	makeLeader(n1)

	// 手工构造：日志含一条旧任期条目 + 一条当前任期条目。
	n1.Log = []Entry{{Term: 0}, {Term: 1, Command: "old"}}
	n1.CurrentTerm = 2
	n1.Log = append(n1.Log, Entry{Term: 2, Command: "cur"})
	n1.nextIndex[2] = 3
	n1.nextIndex[3] = 3
	n1.matchIndex[2] = 0
	n1.matchIndex[3] = 0
	env1.msgs = nil

	// 节点2确认到索引2（旧+当前条目都复制了）；节点3只确认到索引1。
	n1.Step(Message{Type: MsgAppResp, From: 2, To: 1, Term: 2, Success: true, MatchIndex: 2})
	if n1.commitIndex != 2 {
		t.Fatalf("current-term entry at 2 should commit with majority, got %d", n1.commitIndex)
	}

	// 负向用例：旧任期条目在多数派，但当前任期条目只有领导者自己有 -> 不提交任何条目。
	nOld, _, _ := newTestNode(1)
	makeLeader(nOld)
	nOld.CurrentTerm = 3
	nOld.Log = []Entry{{Term: 0}, {Term: 1, Command: "old"}, {Term: 3, Command: "cur"}}
	nOld.commitIndex = 0
	nOld.nextIndex[2] = 2
	nOld.nextIndex[3] = 2
	nOld.matchIndex[2] = 1 // 节点2只有旧条目
	nOld.matchIndex[3] = 1 // 节点3只有旧条目
	nOld.advanceCommit()
	if nOld.commitIndex != 0 {
		t.Fatalf("old-term entry must not be committed from new term before current-term entry replicates, got commit=%d", nOld.commitIndex)
	}
}

// TestLogConflictTruncation 验证跟随者在冲突时截断并覆盖后续日志。
func TestLogConflictTruncation(t *testing.T) {
	n2, _, _ := newTestNode(2)
	// 跟随者日志：[s0, t1:a, t1:b]（索引2是节点1不会有的分叉内容）。
	n2.Log = []Entry{{Term: 0}, {Term: 1, Command: "a"}, {Term: 1, Command: "b-fork"}}

	// 领导者（任期2）以索引1任期1为前缀，追加索引2的新内容。
	app := Message{Type: MsgApp, From: 1, To: 2, Term: 2,
		PrevLogIdx: 1, PrevLogTerm: 1, LeaderCommit: 0,
		Entries: []Entry{{Term: 2, Command: "c"}}}
	n2.Step(app)
	snap := n2.Inspect()
	want := []string{"a", "c"}
	if len(snap.Log)-1 != 2 {
		t.Fatalf("expected 2 entries, got %d: %+v", len(snap.Log)-1, snap.Log)
	}
	for i, cmd := range want {
		if snap.Log[i+1].Command != cmd {
			t.Fatalf("entry %d want %q got %q", i+1, cmd, snap.Log[i+1].Command)
		}
	}
}

// TestPersistAndRestart 验证任期/投票/日志落盘后可从存储恢复。
func TestPersistAndRestart(t *testing.T) {
	st := newMemStore()
	env := &testEnv{}
	n := NewNode(testCfg(1), env, st, newRand(3))
	n.CurrentTerm = 7
	n.VotedFor = 2
	n.Log = append(n.Log, Entry{Term: 5, Command: "persisted"})
	n.persist()

	env2 := &testEnv{}
	n2 := NewNode(testCfg(1), env2, st, newRand(4))
	snap := n2.Inspect()
	if snap.Term != 7 || n2.VotedFor != 2 {
		t.Fatalf("term/vote not restored: term=%d vote=%d", snap.Term, n2.VotedFor)
	}
	if len(snap.Log) != 2 || snap.Log[1].Command != "persisted" {
		t.Fatalf("log not restored: %+v", snap.Log)
	}
}
