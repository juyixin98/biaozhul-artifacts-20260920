package raft_test

import (
	"math/rand"
	"testing"

	"raftdemo/internal/logstore"
	"raftdemo/internal/network"
	"raftdemo/internal/raft"
)

// cluster 是测试用三节点驱动：把 raft.Node 与 network.Network 直接对接。
// 该文件使用外部测试包 raft_test，因为 network 反向依赖 raft，
// 同包测试会形成导入环。
type cluster struct {
	t       *testing.T
	nodes   []*raft.Node
	stores  []logstore.Store
	network *network.Network
	now     int
}

func newCluster(t *testing.T, win [][2]int, seed int64) *cluster {
	t.Helper()
	n := 3
	nw := network.New(n, rand.New(rand.NewSource(seed+999)))
	stores := make([]logstore.Store, n)
	nodes := make([]*raft.Node, n)
	c := &cluster{t: t, nodes: nodes, stores: stores, network: nw}
	for id := 0; id < n; id++ {
		stores[id] = logstore.NewMemStore()
		node, err := raft.NewNode(raft.Config{
			ID: id, N: n, HeartbeatTicks: 3,
			ElecLo: win[id][0], ElecHi: win[id][1],
			Seed: seed + int64(id)*7919 + 1,
		}, stores[id])
		if err != nil {
			t.Fatal(err)
		}
		node.Send = func(now, from, to int, m raft.Msg) { nw.Send(now, m) }
		nodes[id] = node
	}
	return c
}

// step 推进一个 tick：先投递到期消息，再驱动时钟。
func (c *cluster) step() {
	c.now++
	for _, m := range c.network.Tick(c.now) {
		if c.nodes[m.To].Alive() {
			c.nodes[m.To].Handle(c.now, m)
		}
	}
	for _, n := range c.nodes {
		n.Tick(c.now)
	}
}

func (c *cluster) runUntil(maxTicks int, cond func() bool) bool {
	for i := 0; i < maxTicks; i++ {
		if cond() {
			return true
		}
		c.step()
	}
	return cond()
}

func (c *cluster) leader() *raft.Node {
	var best *raft.Node
	for _, n := range c.nodes {
		if n.Alive() && n.Role() == raft.Leader {
			if best == nil || n.Term() > best.Term() {
				best = n
			}
		}
	}
	return best
}

func (c *cluster) committedEqual() bool {
	var ref []logstore.Entry
	for _, n := range c.nodes {
		if !n.Alive() {
			continue
		}
		cur := n.Committed()
		if ref == nil {
			ref = cur
			continue
		}
		common := len(cur)
		if len(ref) < common {
			common = len(ref)
		}
		for k := 0; k < common; k++ {
			if cur[k].Term != ref[k].Term || cur[k].Data != ref[k].Data {
				return false
			}
		}
	}
	return true
}

func TestElectSingleLeader(t *testing.T) {
	c := newCluster(t, [][2]int{{5, 6}, {8, 9}, {10, 11}}, 1)
	if !c.runUntil(30, func() bool { return c.leader() != nil }) {
		t.Fatal("30 tick 内未选出领导者")
	}
	leaders := map[int]int{} // term -> leader
	for _, n := range c.nodes {
		if n.Role() == raft.Leader {
			if prev, ok := leaders[n.Term()]; ok && prev != n.ID() {
				t.Fatalf("同任期出现两个 leader: 节点 %d 和 %d", prev, n.ID())
			}
			leaders[n.Term()] = n.ID()
		}
	}
	if len(leaders) != 1 {
		t.Fatalf("期望恰好一个在任领导者，得到 term->leader=%v", leaders)
	}
}

func TestReplicateAndCommitCurrentTerm(t *testing.T) {
	c := newCluster(t, [][2]int{{5, 6}, {8, 9}, {10, 11}}, 2)
	c.runUntil(30, func() bool { return c.leader() != nil })
	ld := c.leader()
	if !ld.Submit("k1", "v1", c.now) {
		t.Fatal("领导者拒绝了写入")
	}
	// 等待全部节点提交到索引 2（索引 1 为当选 noop）。
	ok := c.runUntil(20, func() bool {
		for _, n := range c.nodes {
			if n.CommitIndex() < 2 {
				return false
			}
		}
		return true
	})
	if !ok {
		t.Fatal("写入未在 20 tick 内复制到全部节点")
	}
	for _, n := range c.nodes {
		log := n.LogSnapshot()
		if log[1].Data != "" || log[1].Term != ld.Term() {
			t.Fatalf("节点 %d 索引1不是本任期 noop: %+v", n.ID(), log[1])
		}
		if log[2].Data != "v1" || log[2].Term != ld.Term() {
			t.Fatalf("节点 %d 索引2不是客户端数据 v1: %+v", n.ID(), log[2])
		}
	}
}

// TestCommitRuleCurrentTermOnly 验证 §5.4.2：旧任期条目不能仅靠副本数提交，
// 必须随当前任期条目一并提交。
func TestCommitRuleCurrentTermOnly(t *testing.T) {
	c := newCluster(t, [][2]int{{4, 5}, {7, 8}, {9, 10}}, 3)
	c.runUntil(20, func() bool {
		l := c.leader()
		return l != nil && l.ID() == 0 &&
			c.nodes[1].CommitIndex() >= 1 && c.nodes[2].CommitIndex() >= 1
	})
	if c.leader() == nil || c.leader().ID() != 0 {
		t.Fatalf("预期 node0 当选，实际 %v", c.leader())
	}
	c.network.Isolate(0, false)
	// node0 在旧任期追加客户端数据，但无法获得多数派，绝不能提交。
	if !c.nodes[0].Submit("stale", "old-term-entry", c.now) {
		t.Fatal("node0 仍是本分区 leader，应接受写入（但不能提交）")
	}
	for i := 0; i < 20; i++ {
		c.step()
		if c.nodes[0].CommitIndex() >= 2 {
			t.Fatal("多数派失联期间旧任期条目被错误提交")
		}
	}
	// 恢复网络后日志必须收敛，且所有节点的已提交前缀逐索引一致。
	c.network.Isolate(0, true)
	ok := c.runUntil(60, func() bool {
		if !c.committedEqual() {
			return false
		}
		for _, n := range c.nodes {
			if n.CommitIndex() < 2 {
				return false
			}
		}
		return true
	})
	if !ok {
		t.Fatal("网络恢复后日志未在 60 tick 内收敛一致")
	}
}

// TestConflictTruncation 验证 §5.3：follower 同索引不同任期的旧条目被截断覆盖。
func TestConflictTruncation(t *testing.T) {
	c := newCluster(t, [][2]int{{4, 5}, {6, 7}, {8, 9}}, 4)
	c.runUntil(20, func() bool {
		l := c.leader()
		return l != nil && c.nodes[1].CommitIndex() >= 1 && c.nodes[2].CommitIndex() >= 1
	})
	old := c.leader()
	c.network.Isolate(old.ID(), false)
	// 其余节点选出更高任期的新 leader 并产生一条已提交写入。
	c.runUntil(40, func() bool {
		l := c.leader()
		return l != nil && l.ID() != old.ID()
	})
	newLeader := c.leader()
	if !newLeader.Submit("new", "committed-new", c.now) {
		t.Fatal("新 leader 拒绝写入")
	}
	c.runUntil(20, func() bool {
		for _, n := range c.nodes {
			if n.ID() == old.ID() {
				continue
			}
			if n.CommitIndex() < newLeader.LastLogIndex() {
				return false
			}
		}
		return true
	})
	// 旧 leader 在隔离期本地写入陈旧后缀。
	old.Submit("ghost", "stale-suffix", c.now)
	c.network.Isolate(old.ID(), true)
	ok := c.runUntil(60, func() bool {
		ref := c.nodes[newLeader.ID()].LogSnapshot()
		o := old.LogSnapshot()
		if len(o) != len(ref) {
			return false
		}
		for k := 1; k < len(ref); k++ {
			if o[k].Term != ref[k].Term || o[k].Data != ref[k].Data {
				return false
			}
		}
		return true
	})
	if !ok {
		var got [][]logstore.Entry
		for _, n := range c.nodes {
			got = append(got, n.LogSnapshot())
		}
		t.Fatalf("恢复后旧 leader 日志未被截断覆盖一致: %v", got)
	}
}

// TestPersistedVoteAndTerm 验证崩溃重启后任期与投票持久化。
func TestPersistedVoteAndTerm(t *testing.T) {
	c := newCluster(t, [][2]int{{4, 5}, {6, 7}, {8, 9}}, 5)
	c.runUntil(20, func() bool { return c.leader() != nil })
	ld := c.leader()
	termBefore := ld.Term()
	fid := -1
	for _, n := range c.nodes {
		if n.ID() != ld.ID() {
			fid = n.ID()
			break
		}
	}
	f := c.nodes[fid]
	storedTerm := f.Term()
	f.Crash()
	if err := f.Restart(c.now); err != nil {
		t.Fatal(err)
	}
	if f.Term() != storedTerm {
		t.Fatalf("重启后任期丢失: %d vs %d", f.Term(), storedTerm)
	}
	if f.Role() != raft.Follower {
		t.Fatal("重启后角色应为 follower")
	}
	// 重放当前任期的 RequestVote；follower 已持久化选票，不能改变投票对象。
	st, _ := f.Store().Load()
	voteBefore := st.VotedFor
	f.Handle(c.now, raft.Msg{
		Type: raft.MsgVoteReq, Term: termBefore, From: (ld.ID() + 1) % 3,
		LastLogIndex: 1, LastLogTerm: termBefore,
	})
	st2, _ := f.Store().Load()
	if st2.VotedFor != voteBefore {
		t.Fatalf("同任期内重启节点的选票被改写: %d -> %d", voteBefore, st2.VotedFor)
	}
}

// TestStaleLeaderStepsDown 验证旧领导者收到更高任期消息后下台。
func TestStaleLeaderStepsDown(t *testing.T) {
	c := newCluster(t, [][2]int{{4, 5}, {6, 7}, {8, 9}}, 6)
	c.runUntil(20, func() bool {
		l := c.leader()
		return l != nil && c.nodes[1].CommitIndex() >= 1 && c.nodes[2].CommitIndex() >= 1
	})
	old := c.leader()
	oldTerm := old.Term()
	c.network.Isolate(old.ID(), false)
	c.runUntil(50, func() bool {
		for _, n := range c.nodes {
			if n.ID() != old.ID() && n.Term() > oldTerm {
				return true
			}
		}
		return false
	})
	higher := oldTerm + 1
	for _, n := range c.nodes {
		if n.ID() != old.ID() && n.Term() > higher {
			higher = n.Term()
		}
	}
	// 模拟旧 leader 恢复接触到新任期：收到一条更高任期的 AppendEntries 响应。
	old.Handle(c.now, raft.Msg{
		Type: raft.MsgAppResp, Term: higher, From: (old.ID() + 1) % 3,
		Success: false, ConflictIndex: 1,
	})
	if old.Role() != raft.Follower || old.Term() != higher {
		t.Fatalf("旧 leader 未在更高任期下台: role=%s term=%d", old.Role(), old.Term())
	}
	if old.Submit("x", "y", c.now) {
		t.Fatal("已下台的旧 leader 不应接受客户端写入")
	}
}
