package raft

import "math/rand"

func newRand(seed int64) *rand.Rand { return rand.New(rand.NewSource(seed)) }

// makeLeader 让节点直接走完候选人流程（在无网络测试中使用）。
func makeLeader(n *Node) {
	n.role = Candidate
	n.CurrentTerm++
	n.VotedFor = n.cfg.ID
	n.grantedVotes = map[int]bool{n.cfg.ID: true}
	n.becomeLeader()
}
