package raft

// JSON-friendly DTOs for messages and node state.

type voteView struct {
	Term         int  `json:"term"`
	CandidateID  int  `json:"candidateId,omitempty"`
	LastLogIndex int  `json:"lastLogIndex,omitempty"`
	LastLogTerm  int  `json:"lastLogTerm,omitempty"`
	Grant        bool `json:"grant,omitempty"`
}

func viewVote(rv RequestVote) voteView {
	return voteView{
		Term:         rv.Term,
		CandidateID:  rv.CandidateID,
		LastLogIndex: rv.LastLogIndex,
		LastLogTerm:  rv.LastLogTerm,
		Grant:        rv.Grant,
	}
}

type appendView struct {
	Term     int         `json:"term"`
	LeaderID int         `json:"leaderId"`
	PrevLogI int         `json:"prevLogIndex"`
	PrevLogT int         `json:"prevLogTerm"`
	Entries  []EntryView `json:"entries"`
	Commit   int         `json:"leaderCommit"`
	Success  bool        `json:"success,omitempty"`
	Ack      int         `json:"ack,omitempty"`
}

func viewAppend(ae AppendEntries) appendView {
	v := appendView{
		Term:     ae.Term,
		LeaderID: ae.LeaderID,
		PrevLogI: ae.PrevLogI,
		PrevLogT: ae.PrevLogT,
		Commit:   ae.Commit,
		Success:  ae.Success,
		Ack:      ae.Ack,
	}
	for _, e := range ae.Entries {
		v.Entries = append(v.Entries, EntryView{Term: e.Term, Index: e.Index, Command: e.Command})
	}
	return v
}

// NodeView is the external snapshot of one node.
type NodeView struct {
	ID          int               `json:"id"`
	Alive       bool              `json:"alive"`
	Role        string            `json:"role"`
	Term        int               `json:"term"`
	VotedFor    int               `json:"votedFor"`
	LeaderHint  int               `json:"leaderHint"`
	CommitIndex int               `json:"commitIndex"`
	LastIndex   int               `json:"lastIndex"`
	Log         []EntryView       `json:"log"`
	Committed   []EntryView       `json:"committed"`
	KV          map[string]string `json:"kv"`
}

// View builds the snapshot for id.
func (s *Simulator) View(id int) NodeView {
	n := s.nodes[id]
	v := NodeView{
		ID:          id,
		Alive:       s.alive[id],
		Role:        n.Role().String(),
		Term:        n.Term(),
		VotedFor:    n.votedFor,
		LeaderHint:  n.leaderID,
		CommitIndex: n.commitIdx,
		LastIndex:   n.LastIndex(),
		KV:          n.KV(),
	}
	for i := 1; i < len(n.log); i++ {
		v.Log = append(v.Log, EntryView{Term: n.log[i].Term, Index: n.log[i].Index, Command: n.log[i].Command})
	}
	for i := 1; i <= n.commitIdx && i < len(n.log); i++ {
		v.Committed = append(v.Committed, EntryView{Term: n.log[i].Term, Index: n.log[i].Index, Command: n.log[i].Command})
	}
	return v
}

// Views snapshots all nodes.
func (s *Simulator) Views() []NodeView {
	out := make([]NodeView, 0, len(s.ids))
	for _, id := range s.ids {
		out = append(out, s.View(id))
	}
	return out
}
