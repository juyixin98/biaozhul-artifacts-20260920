// Package raft implements a deterministic, tick-driven Raft node.
//
// The node is single-threaded: callers drive it exclusively through Tick
// (timers) and Step (incoming messages), both of which return any messages
// that should be sent on the (simulated) network. There is no goroutine and
// no real clock inside the node: time is an integer tick counter, and all
// randomness (election-timeout jitter) comes from an injected random source.
package raft

import (
	"errors"
	"fmt"
	"math/rand"
	"sort"
)

// Role of a node.
type Role int

const (
	Follower Role = iota
	Candidate
	Leader
)

func (r Role) String() string {
	switch r {
	case Follower:
		return "follower"
	case Candidate:
		return "candidate"
	case Leader:
		return "leader"
	default:
		return "unknown"
	}
}

// Entry is one log entry. A log is 1-indexed; entry with log index i lives at
// slice position i-1.
type Entry struct {
	Term    int    `json:"term"`
	Command string `json:"command"`
}

// RNG is the subset of math/rand.Rand that the node needs.
type RNG interface {
	Intn(n int) int
}

// Config configures a node. Time values are expressed in ticks.
type Config struct {
	// Nodes are the cluster member IDs, sorted ascending.
	Nodes []int
	// ElectionMin/Max bound the random election timeout: [Min, Max).
	ElectionMin int
	ElectionMax int
	// Heartbeat is the leader heartbeat interval.
	Heartbeat int
	// FixedTimeout, when set for a node ID, pins that node's election
	// timeout (no randomness). Used by fully deterministic scenarios.
	FixedTimeout map[int]int
	// BuggyOldTermCommit reproduces the Figure 8 bug: a leader commits an
	// entry from a previous term as soon as it is replicated to a majority,
	// instead of waiting until an entry from its own term is replicated.
	BuggyOldTermCommit bool
}

func (c Config) quorum() int { return len(c.Nodes)/2 + 1 }

// Message is an RPC request or response exchanged between nodes.
type Message struct {
	Type    string  `json:"type"` // RequestVote | RequestVoteReply | AppendEntries | AppendEntriesReply
	From    int     `json:"from"`
	To      int     `json:"to"`
	Term    int     `json:"term"`
	Entries []Entry `json:"entries,omitempty"`

	// RequestVote
	LastLogIndex int `json:"lastLogIndex,omitempty"`
	LastLogTerm  int `json:"lastLogTerm,omitempty"`

	// AppendEntries
	PrevLogIndex int `json:"prevLogIndex,omitempty"`
	PrevLogTerm  int `json:"prevLogTerm,omitempty"`
	LeaderCommit int `json:"leaderCommit,omitempty"`

	// replies
	Grant bool `json:"grant,omitempty"`
	// HintTerm/HintIndex speed up log backtracking on AppendEntries failure.
	HintTerm  int `json:"hintTerm,omitempty"`
	HintIndex int `json:"hintIndex,omitempty"`
}

const (
	MsgRequestVote        = "RequestVote"
	MsgRequestVoteReply   = "RequestVoteReply"
	MsgAppendEntries      = "AppendEntries"
	MsgAppendEntriesReply = "AppendEntriesReply"
)

// FSM is the replicated state machine. Apply is invoked in order, once per
// committed entry (also replayed on restart up to the persisted applied index).
type FSM interface {
	Apply(index, term int, command string)
}

// Node is a single Raft member.
type Node struct {
	id  int
	cfg Config
	rng RNG
	fsm FSM

	// Persistent state (mirrored in the Storage).
	currentTerm int
	votedFor    int // -1 when this node has not voted in the current term
	log         []Entry

	// Volatile state.
	role             Role
	alive            bool
	commitIndex      int
	lastApplied      int
	leaderID         int
	electionElapsed  int
	heartbeatElapsed int
	electionTimeout  int
	nextIndex        map[int]int
	matchIndex       map[int]int
	votes            map[int]bool

	storage Storage
}

// NewNode constructs a node and loads its persistent state (fresh storage
// yields an unstarted follower in term 0).
func NewNode(id int, cfg Config, rng RNG, storage Storage, fsm FSM) (*Node, error) {
	n := &Node{
		id:         id,
		cfg:        cfg,
		rng:        rng,
		fsm:        fsm,
		votedFor:   -1,
		leaderID:   -1,
		storage:    storage,
		nextIndex:  map[int]int{},
		matchIndex: map[int]int{},
		votes:      map[int]bool{},
	}
	if err := n.load(); err != nil {
		return nil, err
	}
	n.alive = true
	n.role = Follower
	n.resetElectionTimer()
	return n, nil
}

// ---- persistent state -----------------------------------------------------

func (n *Node) persist() {
	if err := n.storage.Save(PersistentState{
		CurrentTerm:  n.currentTerm,
		VotedFor:     n.votedFor,
		Log:          append([]Entry(nil), n.log...),
		CommitIndex:  n.commitIndex,
		AppliedIndex: n.lastApplied,
	}); err != nil {
		panic(fmt.Errorf("node %d persist: %w", n.id, err))
	}
}

func (n *Node) load() error {
	st, err := n.storage.Load()
	if errors.Is(err, ErrEmptyStorage) {
		// Brand-new node: start with an empty log and no vote.
		n.votedFor = -1
		n.log = []Entry{}
		return nil
	}
	if err != nil {
		return err
	}
	n.currentTerm = st.CurrentTerm
	n.votedFor = st.VotedFor
	if n.votedFor == 0 && st.CurrentTerm != 0 {
		// JSON encodes a -1 vote explicitly; guard malformed/old files.
		n.votedFor = -1
	}
	n.log = st.Log
	n.commitIndex = st.CommitIndex
	n.lastApplied = st.AppliedIndex
	// Replay the committed prefix into a fresh state machine.
	for i := 1; i <= n.lastApplied; i++ {
		e := n.entry(i)
		n.fsm.Apply(i, e.Term, e.Command)
	}
	return nil
}

// ---- lifecycle ------------------------------------------------------------

// Stop powers the node off. Timers and messages are ignored until Restart.
func (n *Node) Stop() { n.alive = false }

// Restart powers the node back on, reloading all state from storage and
// resetting the volatile leader/candidate state exactly like a real reboot.
func (n *Node) Restart() error {
	n.alive = true
	n.role = Follower
	n.leaderID = -1
	n.nextIndex = map[int]int{}
	n.matchIndex = map[int]int{}
	n.votes = map[int]bool{}
	n.electionElapsed = 0
	n.heartbeatElapsed = 0
	return n.load()
}

func (n *Node) Alive() bool { return n.alive }

// ---- log helpers (1-indexed) ---------------------------------------------

func (n *Node) lastIndex() int { return len(n.log) }

func (n *Node) lastTerm() int {
	if len(n.log) == 0 {
		return 0
	}
	return n.log[len(n.log)-1].Term
}

// entry returns the entry at 1-based index i (i must be >= 1 and <= lastIndex).
func (n *Node) entry(i int) Entry { return n.log[i-1] }

func (n *Node) termAt(i int) int {
	if i < 1 {
		return 0
	}
	if i > len(n.log) {
		return -1
	}
	return n.log[i-1].Term
}

func (n *Node) isUpToDate(lastIndex, lastTerm int) bool {
	if lastTerm != n.lastTerm() {
		return lastTerm > n.lastTerm()
	}
	return lastIndex >= n.lastIndex()
}

// ---- timers ---------------------------------------------------------------

func (n *Node) resetElectionTimer() {
	if t, ok := n.cfg.FixedTimeout[n.id]; ok {
		n.electionTimeout = t
		return
	}
	span := n.cfg.ElectionMax - n.cfg.ElectionMin
	if span <= 0 {
		n.electionTimeout = n.cfg.ElectionMin
		return
	}
	n.electionTimeout = n.cfg.ElectionMin + n.rng.Intn(span)
}

// Tick advances the node by one tick and returns messages the timer produced.
func (n *Node) Tick() []Message {
	if !n.alive {
		return nil
	}
	switch n.role {
	case Leader:
		n.heartbeatElapsed++
		if n.heartbeatElapsed >= n.cfg.Heartbeat {
			n.heartbeatElapsed = 0
			return n.broadcastAppend()
		}
	case Follower, Candidate:
		n.electionElapsed++
		if n.electionElapsed >= n.electionTimeout {
			return n.startElection()
		}
	}
	return nil
}

// ---- elections ------------------------------------------------------------

func (n *Node) becomeFollower(term int) {
	n.role = Follower
	if term > n.currentTerm {
		n.currentTerm = term
		n.votedFor = -1
	}
	n.leaderID = -1
	n.electionElapsed = 0
	n.resetElectionTimer()
	n.persist()
}

func (n *Node) startElection() []Message {
	n.role = Candidate
	n.currentTerm++
	n.votedFor = n.id
	n.leaderID = -1
	n.electionElapsed = 0
	n.resetElectionTimer()
	n.votes = map[int]bool{n.id: true}
	n.persist()

	msgs := []Message{}
	for _, p := range n.cfg.Nodes {
		if p == n.id {
			continue
		}
		msgs = append(msgs, Message{
			Type:         MsgRequestVote,
			From:         n.id,
			To:           p,
			Term:         n.currentTerm,
			LastLogIndex: n.lastIndex(),
			LastLogTerm:  n.lastTerm(),
		})
	}
	if len(n.cfg.Nodes) == 1 {
		n.becomeLeader()
	}
	return msgs
}

func (n *Node) becomeLeader() {
	n.role = Leader
	n.leaderID = n.id
	n.heartbeatElapsed = n.cfg.Heartbeat // send a heartbeat on the next tick
	last := n.lastIndex()
	for _, p := range n.cfg.Nodes {
		if p == n.id {
			continue
		}
		n.nextIndex[p] = last + 1
		n.matchIndex[p] = 0
	}
	n.votes = map[int]bool{}
}

func (n *Node) handleRequestVote(m Message) []Message {
	reply := Message{Type: MsgRequestVoteReply, From: n.id, To: m.From, Term: n.currentTerm}
	if m.Term < n.currentTerm {
		return []Message{reply}
	}
	if m.Term > n.currentTerm {
		n.becomeFollower(m.Term)
	}
	if n.role == Candidate {
		// Already voted for self in this term.
		return []Message{reply}
	}
	if (n.votedFor == -1 || n.votedFor == m.From) && n.isUpToDate(m.LastLogIndex, m.LastLogTerm) {
		n.votedFor = m.From
		n.role = Follower
		n.leaderID = -1
		n.electionElapsed = 0
		n.resetElectionTimer()
		n.persist()
		reply.Grant = true
	}
	reply.Term = n.currentTerm
	return []Message{reply}
}

func (n *Node) handleRequestVoteReply(m Message) []Message {
	if n.role != Candidate || m.Term != n.currentTerm {
		return nil
	}
	if m.Grant {
		n.votes[m.From] = true
		if len(n.votes) >= n.cfg.quorum() {
			n.becomeLeader()
		}
	}
	return nil
}

// ---- log replication ------------------------------------------------------

// Propose appends a command to the leader's log. It returns false (and no
// messages) when this node is not currently the leader.
func (n *Node) Propose(command string) ([]Message, bool) {
	if !n.alive || n.role != Leader {
		return nil, false
	}
	n.log = append(n.log, Entry{Term: n.currentTerm, Command: command})
	n.matchIndex[n.id] = n.lastIndex()
	n.persist()
	return n.broadcastAppend(), true
}

func (n *Node) broadcastAppend() []Message {
	msgs := make([]Message, 0, len(n.cfg.Nodes)-1)
	for _, p := range n.cfg.Nodes {
		if p == n.id {
			continue
		}
		msgs = append(msgs, n.appendTo(p))
	}
	return msgs
}

func (n *Node) appendTo(peer int) Message {
	prev := n.nextIndex[peer] - 1
	entries := []Entry{}
	if prev < n.lastIndex() {
		entries = append(entries, n.log[prev:]...)
	}
	return Message{
		Type:         MsgAppendEntries,
		From:         n.id,
		To:           peer,
		Term:         n.currentTerm,
		PrevLogIndex: prev,
		PrevLogTerm:  n.termAt(prev),
		Entries:      entries,
		LeaderCommit: n.commitIndex,
	}
}

func (n *Node) handleAppendEntries(m Message) []Message {
	reply := Message{Type: MsgAppendEntriesReply, From: n.id, To: m.From, Term: n.currentTerm}

	if m.Term < n.currentTerm {
		return []Message{reply}
	}
	if m.Term > n.currentTerm {
		n.becomeFollower(m.Term)
	}
	// A legitimate leader in this term: acknowledge leadership and stay alive.
	if n.role != Follower {
		n.role = Follower
		n.leaderID = m.From
	}
	n.leaderID = m.From
	n.electionElapsed = 0
	n.resetElectionTimer()

	// Consistency check on the entry immediately before the new entries.
	if m.PrevLogIndex > n.lastIndex() {
		reply.HintIndex = n.lastIndex() + 1
		reply.HintTerm = 0
		return []Message{reply}
	}
	if t := n.termAt(m.PrevLogIndex); t != m.PrevLogTerm {
		reply.HintTerm = t
		// First index in the conflicting term.
		i := m.PrevLogIndex
		for i > 1 && n.termAt(i-1) == t {
			i--
		}
		reply.HintIndex = i
		return []Message{reply}
	}

	// Merge: truncate at the first conflicting existing entry, then append.
	changed := false
	for i, e := range m.Entries {
		idx := m.PrevLogIndex + 1 + i
		if idx <= n.lastIndex() {
			if n.termAt(idx) == e.Term {
				continue // identical prefix entry
			}
			n.log = n.log[:idx-1] // conflict: drop this entry and the tail
		}
		n.log = append(n.log, m.Entries[i:]...)
		changed = true
		break
	}
	if changed {
		n.persist()
	}

	if m.LeaderCommit > n.commitIndex {
		n.commitIndex = min(m.LeaderCommit, n.lastIndex())
		n.applyCommitted()
	}
	reply.Term = n.currentTerm
	reply.Grant = true
	reply.PrevLogIndex = m.PrevLogIndex
	reply.Entries = m.Entries
	return []Message{reply}
}

func (n *Node) handleAppendEntriesReply(m Message) []Message {
	if n.role != Leader {
		return nil
	}
	if m.Term > n.currentTerm {
		n.becomeFollower(m.Term)
		return nil
	}
	if m.Term < n.currentTerm {
		return nil
	}
	peer := m.From
	// A reply for an AppendEntries this leadership did not send (e.g. a
	// delayed reply left over from an earlier term or an older leader) must
	// be ignored: we have no replication state for this peer yet.
	if _, known := n.nextIndex[peer]; !known {
		return nil
	}
	if m.Grant {
		acked := m.PrevLogIndex + len(m.Entries)
		if acked > n.matchIndex[peer] {
			n.matchIndex[peer] = acked
			n.nextIndex[peer] = acked + 1
		}
		return n.maybeCommit()
	}
	// Back the follower's nextIndex off using its conflict hint.
	if m.HintTerm > 0 {
		// Last index in our log with the conflicting term.
		back := 0
		for i := n.lastIndex(); i >= 1; i-- {
			if n.termAt(i) == m.HintTerm {
				back = i
				break
			}
		}
		if back > 0 {
			n.nextIndex[peer] = back + 1
		} else {
			// We have no entry of that term: back up to the follower's
			// hinted index, but never below the start of the 1-based log.
			n.nextIndex[peer] = max(1, m.HintIndex)
		}
	} else {
		n.nextIndex[peer] = max(1, m.HintIndex)
	}
	return []Message{n.appendTo(peer)}
}

// maybeCommit advances the leader commit index from follower acks.
func (n *Node) maybeCommit() []Message {
	for N := n.lastIndex(); N > n.commitIndex; N-- {
		replicated := 0
		for _, p := range n.cfg.Nodes {
			mi := n.matchIndex[p]
			if p == n.id {
				mi = n.lastIndex()
			}
			if mi >= N {
				replicated++
			}
		}
		if replicated < n.cfg.quorum() {
			continue
		}
		e := n.entry(N)
		// Raft §5.4.2: a leader only commits entries from its *current* term
		// by the majority rule; older-term entries get committed implicitly
		// once a current-term entry is committed. BuggyOldTermCommit drops
		// that restriction (Figure 8).
		if e.Term != n.currentTerm && !n.cfg.BuggyOldTermCommit {
			break
		}
		n.commitIndex = N
		n.applyCommitted()
		return n.broadcastAppend()
	}
	return nil
}

func (n *Node) applyCommitted() {
	for n.lastApplied < n.commitIndex {
		n.lastApplied++
		e := n.entry(n.lastApplied)
		n.fsm.Apply(n.lastApplied, e.Term, e.Command)
	}
	n.persist()
}

// Step delivers one RPC (request or response) and returns replies.
func (n *Node) Step(m Message) []Message {
	if !n.alive {
		return nil
	}
	// Any RPC with a higher term makes a leader/candidate step down.
	if m.Term > n.currentTerm && (m.Type == MsgRequestVote || m.Type == MsgAppendEntries) {
		n.becomeFollower(m.Term)
	}
	switch m.Type {
	case MsgRequestVote:
		return n.handleRequestVote(m)
	case MsgRequestVoteReply:
		return n.handleRequestVoteReply(m)
	case MsgAppendEntries:
		return n.handleAppendEntries(m)
	case MsgAppendEntriesReply:
		return n.handleAppendEntriesReply(m)
	default:
		panic(fmt.Sprintf("unknown message type %q", m.Type))
	}
}

// ---- introspection (used by the simulator and the checker) ----------------

// Snapshot is a JSON-friendly view of node state.
type Snapshot struct {
	ID          int     `json:"id"`
	Alive       bool    `json:"alive"`
	Role        string  `json:"role"`
	Term        int     `json:"term"`
	VotedFor    int     `json:"votedFor"`
	LeaderID    int     `json:"leaderId"`
	CommitIndex int     `json:"commitIndex"`
	LastApplied int     `json:"lastApplied"`
	Log         []Entry `json:"log"`
	Committed   []Entry `json:"committed"`
}

func (n *Node) Snapshot() Snapshot {
	role := "down"
	if n.alive {
		role = n.role.String()
	}
	log := append([]Entry{}, n.log...)
	if log == nil {
		log = []Entry{}
	}
	committed := []Entry{}
	if n.commitIndex >= 1 {
		committed = append(committed, n.log[:n.commitIndex]...)
	}
	return Snapshot{
		ID: n.id, Alive: n.alive, Role: role, Term: n.currentTerm,
		VotedFor: n.votedFor, LeaderID: n.leaderID,
		CommitIndex: n.commitIndex, LastApplied: n.lastApplied,
		Log: log, Committed: committed,
	}
}

func (n *Node) ID() int          { return n.id }
func (n *Node) Role() Role       { return n.role }
func (n *Node) Term() int        { return n.currentTerm }
func (n *Node) CommitIndex() int { return n.commitIndex }
func (n *Node) LastIndex() int   { return n.lastIndex() }
func (n *Node) LeaderID() int    { return n.leaderID }

// LogEntries returns a copy of the full log.
func (n *Node) LogEntries() []Entry { return append([]Entry(nil), n.log...) }

// SortedIDs returns the configured node IDs sorted.
func SortedIDs(ids []int) []int {
	out := append([]int(nil), ids...)
	sort.Ints(out)
	return out
}

var _ RNG = (*rand.Rand)(nil)
