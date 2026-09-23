// Package raft implements a small, deterministic Raft log-replication state
// machine designed for simulation (NOT for production use).
//
// There are no goroutines or real timers inside the raft package: every node
// reacts to events fed to it by a Simulator. The simulator owns the virtual
// clock, the in-memory network and an injectable random source used solely for
// election-timeout jitter, which makes every run reproducible.
package raft

import (
	"encoding/gob"
	"fmt"
	"io"
	"math/rand"
	"sort"
)

// Role of a single node.
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

// LogEntry is one replicated command. Index is 1-based; the log holds a
// sentinel at index 0 so lastIndex 0 means "empty log".
type LogEntry struct {
	Term    int
	Index   int
	Command string
}

// EntryView is the immutable JSON-friendly view of a log entry.
type EntryView struct {
	Term    int    `json:"term"`
	Index   int    `json:"index"`
	Command string `json:"command"`
}

// RequestVote is the election RPC, in either direction: the same struct is
// used for the request and the response (Grant/Term populated on the way back).
type RequestVote struct {
	Term         int
	CandidateID  int
	LastLogIndex int
	LastLogTerm  int

	Grant bool
}

// AppendEntries is the log-replication / heartbeat RPC, again reused for the
// response (Success on the way back).
type AppendEntries struct {
	Term     int
	LeaderID int
	PrevLogI int
	PrevLogT int
	Entries  []LogEntry
	Commit   int

	Success bool

	// Ack is populated on success responses: the follower's last log index
	// after applying the request.
	Ack int
}

// AckedIndex returns the follower-acknowledged prefix length from a response.
func (a *AppendEntries) AckedIndex() int { return a.Ack }

// Message kind travelling through the in-memory network.
type MsgKind int

const (
	MsgVoteReq MsgKind = iota
	MsgVoteResp
	MsgAppendReq
	MsgAppendResp
)

func (k MsgKind) String() string {
	switch k {
	case MsgVoteReq:
		return "RequestVote"
	case MsgVoteResp:
		return "RequestVoteResp"
	case MsgAppendReq:
		return "AppendEntries"
	case MsgAppendResp:
		return "AppendEntriesResp"
	default:
		return "?"
	}
}

// Message is one RPC request or response delivered at a virtual time.
type Message struct {
	Kind     MsgKind
	From, To int
	Term     int
	Vote     *RequestVote
	Append   *AppendEntries
	Deliver  int64 // virtual nanosecond timestamp
}

// PersistentState is everything Raft requires to survive a restart. It is
// encoded with gob to a user-supplied writer; storage is deliberately tiny and
// synchronous (the simulator's "disk" is bytes on disk or in memory).
type PersistentState struct {
	CurrentTerm int
	VotedFor    int // -1 = voted for nobody this term
	Log         []LogEntry
}

// Variant toggles protocol semantics. "standard" is the textbook Raft safety
// rules; "naive" deliberately weakens two of them so the enumeration harness
// can produce and replay a real safety counterexample:
//
//   - CommitByOneQuorum: a leader commits an entry once a *single* follower has
//     it (instead of a majority), allowing a partitioned minority leader to
//     commit entries that will later be overwritten.
//   - AppendNoTermCheck: AppendEntries requests are accepted even from a
//     lower-term leader (normally a follower rejects them and the stale leader
//     steps down), letting an old leader's stale delayed messages corrupt a
//     committed prefix — the "old message delay" fault from the brief.
type Variant struct {
	Name              string
	CommitByOneQuorum bool
	AppendNoTermCheck bool
}

// StandardVariant returns the textbook-safe configuration.
func StandardVariant() Variant {
	return Variant{Name: "standard"}
}

// NaiveVariant returns the intentionally buggy configuration.
func NaiveVariant() Variant {
	return Variant{
		Name:              "naive",
		CommitByOneQuorum: true,
		AppendNoTermCheck: true,
	}
}

// StaleAppendVariant keeps the majority-commit rule but skips the
// AppendEntries term check, isolating the "delayed old message" failure.
func StaleAppendVariant() Variant {
	return Variant{Name: "notermcheck", AppendNoTermCheck: true}
}

// Node is a single Raft peer.
type Node struct {
	id      int
	peers   []int
	variant Variant

	// Persistent.
	currentTerm int
	votedFor    int
	log         []LogEntry // index 0 is a sentinel {Term:0,Index:0}

	// Volatile.
	role        Role
	leaderID    int
	commitIdx   int
	lastApplied int
	kv          map[string]string

	votes map[int]bool

	matchIndex map[int]int
	nextIndex  map[int]int

	// Election/heartbeat timer generations: bumping a generation invalidates
	// the currently scheduled timer event (timers are reset, not queued).
	electionGen  int
	heartbeatGen int
}

// Config builds a node set.
type Config struct {
	IDs     []int
	Variant Variant
}

// New constructs a node with an empty log and term 0.
func New(id int, cfg Config) *Node {
	log := []LogEntry{{Term: 0, Index: 0, Command: ""}}
	n := &Node{
		id:         id,
		peers:      append([]int(nil), cfg.IDs...),
		variant:    cfg.Variant,
		votedFor:   -1,
		log:        log,
		role:       Follower,
		leaderID:   -1,
		votes:      map[int]bool{},
		matchIndex: map[int]int{},
		nextIndex:  map[int]int{},
		kv:         map[string]string{},
	}
	return n
}

// Restore constructs a node from its persisted state (see Node.Persist).
func Restore(id int, cfg Config, r io.Reader) (*Node, error) {
	var ps PersistentState
	dec := gob.NewDecoder(r)
	if err := dec.Decode(&ps); err != nil {
		return nil, fmt.Errorf("raft: decode persistent state: %w", err)
	}
	n := New(id, cfg)
	n.currentTerm = ps.CurrentTerm
	n.votedFor = ps.VotedFor
	if len(ps.Log) > 0 {
		n.log = ps.Log
	}
	// Rebuild the KV state machine deterministically from the committed prefix.
	n.applyCommitted()
	return n, nil
}

// Persist writes durable state. Called by the simulator at state-changing
// boundaries before messages acknowledging those changes go out.
func (n *Node) Persist(w io.Writer) error {
	ps := PersistentState{
		CurrentTerm: n.currentTerm,
		VotedFor:    n.votedFor,
		Log:         n.log,
	}
	enc := gob.NewEncoder(w)
	return enc.Encode(&ps)
}

func (n *Node) ID() int          { return n.id }
func (n *Node) Term() int        { return n.currentTerm }
func (n *Node) Role() Role       { return n.role }
func (n *Node) LeaderHint() int  { return n.leaderID }
func (n *Node) CommitIndex() int { return n.commitIdx }
func (n *Node) LastIndex() int   { return n.log[len(n.log)-1].Index }
func (n *Node) Variant() Variant { return n.variant }

// termAt returns the term of the entry at index i; index 0 has term 0.
func (n *Node) termAt(i int) int {
	if i < 0 || i >= len(n.log) {
		return -1
	}
	return n.log[i].Term
}

// lastLogTerm returns the term of the final log entry.
func (n *Node) lastLogTerm() int { return n.termAt(n.LastIndex()) }

func (n *Node) lastLogIndex() int { return n.LastIndex() }

// upToDate implements Raft's RequestVote voter check: candidate log is at
// least as up-to-date as the voter's.
func (n *Node) upToDate(lastIndex, lastTerm int) bool {
	myTerm := n.lastLogTerm()
	if lastTerm != myTerm {
		return lastTerm > myTerm
	}
	return lastIndex >= n.lastLogIndex()
}

func (n *Node) isPeer(id int) bool {
	for _, p := range n.peers {
		if p == id {
			return true
		}
	}
	return false
}

// --- state transitions ------------------------------------------------------

// becomeFollower switches to a follower in term t. If the term increased the
// vote is cleared. It never resets the timer; callers schedule the election
// timer explicitly so the simulator stays the single owner of time.
func (n *Node) becomeFollower(t int) {
	if t > n.currentTerm {
		n.votedFor = -1
	}
	n.currentTerm = t
	n.role = Follower
	n.leaderID = -1
	n.votes = map[int]bool{}
	n.heartbeatGen++
}

// startElection implements a candidate election timeout: advance term, vote
// for self, and broadcast RequestVote.
func (n *Node) startElection(out []Message, now int64, timeout int64) []Message {
	n.currentTerm++
	n.role = Candidate
	n.leaderID = -1
	n.votedFor = n.id
	n.votes = map[int]bool{n.id: true}
	n.electionGen++
	n.heartbeatGen++

	req := &RequestVote{
		Term:         n.currentTerm,
		CandidateID:  n.id,
		LastLogIndex: n.lastLogIndex(),
		LastLogTerm:  n.lastLogTerm(),
	}
	for _, p := range n.peers {
		if p == n.id {
			continue
		}
		out = append(out, Message{
			Kind: MsgVoteReq, From: n.id, To: p, Term: n.currentTerm,
			Vote: req, Deliver: now + timeout,
		})
	}
	return out
}

// becomeLeader runs the leader-state initialization: nextIndex starts at the
// leader's last index + 1, matchIndex at 0, a no-op entry is appended (the
// standard Raft trick to commit entries from previous terms once the no-op
// reaches a majority), and an immediate AppendEntries broadcast is produced.
func (n *Node) becomeLeader(now int64, timeout int64) []Message {
	n.role = Leader
	n.leaderID = n.id
	n.log = append(n.log, LogEntry{Term: n.currentTerm, Index: n.LastIndex() + 1, Command: noopCommand})
	last := n.LastIndex()
	for _, p := range n.peers {
		if p == n.id {
			continue
		}
		n.nextIndex[p] = last
		n.matchIndex[p] = 0
	}
	n.matchIndex[n.id] = last
	n.nextIndex[n.id] = last + 1
	n.votes = map[int]bool{}
	n.electionGen++
	n.heartbeatGen++
	n.advanceCommitIndex()
	n.applyCommitted()
	return n.appendToAll(now, timeout)
}

// appendToAll builds one AppendEntries RPC per peer from the current
// nextIndex/matchIndex bookkeeping.
func (n *Node) appendToAll(now, timeout int64) []Message {
	out := make([]Message, 0, len(n.peers)-1)
	for _, p := range n.peers {
		if p == n.id {
			continue
		}
		prevI := n.nextIndex[p] - 1
		ae := &AppendEntries{
			Term:     n.currentTerm,
			LeaderID: n.id,
			PrevLogI: prevI,
			PrevLogT: n.termAt(prevI),
			Commit:   n.commitIdx,
		}
		for i := prevI + 1; i <= n.LastIndex(); i++ {
			ae.Entries = append(ae.Entries, n.log[i])
		}
		out = append(out, Message{
			Kind: MsgAppendReq, From: n.id, To: p, Term: n.currentTerm,
			Append: ae, Deliver: now + timeout,
		})
	}
	return out
}

// maybeWinElection checks whether the candidate gathered a majority.
func (n *Node) maybeWinElection() bool {
	granted := 0
	for _, v := range n.votes {
		if v {
			granted++
		}
	}
	return granted*2 > len(n.peers)
}

// --- message handling -------------------------------------------------------

// HandleRequestVote runs the voter logic on an inbound RequestVote request.
// The return value is nil when the vote is silently ignored (stale term);
// otherwise it carries the response.
func (n *Node) HandleRequestVote(rv RequestVote, now, timeout int64) *Message {
	if rv.Term < n.currentTerm {
		reply := false
		m := &Message{
			Kind: MsgVoteResp, From: n.id, To: rv.CandidateID,
			Term: n.currentTerm, Vote: &RequestVote{Term: n.currentTerm, Grant: reply},
			Deliver: now + timeout,
		}
		return m
	}
	if rv.Term > n.currentTerm {
		n.becomeFollower(rv.Term)
	}
	grant := false
	if (n.votedFor == -1 || n.votedFor == rv.CandidateID) &&
		n.upToDate(rv.LastLogIndex, rv.LastLogTerm) {
		n.votedFor = rv.CandidateID
		grant = true
	}
	return &Message{
		Kind: MsgVoteResp, From: n.id, To: rv.CandidateID,
		Term: n.currentTerm, Vote: &RequestVote{Term: n.currentTerm, Grant: grant},
		Deliver: now + timeout,
	}
}

// handleVoteResponse processes a RequestVote response at the candidate.
func (n *Node) handleVoteResponse(m Message, now, timeout int64) []Message {
	rv := m.Vote
	if n.role != Candidate {
		return nil
	}
	if rv.Term > n.currentTerm {
		n.becomeFollower(rv.Term)
		return nil
	}
	if rv.Term < n.currentTerm {
		return nil
	}
	if rv.Grant {
		n.votes[m.From] = true
		if n.maybeWinElection() {
			return n.becomeLeader(now, timeout)
		}
	}
	return nil
}

// HandleAppendEntries runs follower/candidate logic for an inbound
// AppendEntries request.
func (n *Node) HandleAppendEntries(ae AppendEntries, now, timeout int64) *Message {
	// The naive variant skips the lower-term rejection; standard Raft rejects
	// stale leaders outright (and the simulator forces them to step down when
	// the response comes back).
	if !n.variant.AppendNoTermCheck && ae.Term < n.currentTerm {
		return &Message{
			Kind: MsgAppendResp, From: n.id, To: ae.LeaderID,
			Term:    n.currentTerm,
			Append:  &AppendEntries{Term: n.currentTerm, Success: false},
			Deliver: now + timeout,
		}
	}
	if ae.Term > n.currentTerm {
		n.becomeFollower(ae.Term)
	}
	if n.role != Follower {
		// A legitimate current-term leader makes us stand down.
		n.becomeFollower(n.currentTerm)
	}
	n.leaderID = ae.LeaderID

	resp := &AppendEntries{Term: n.currentTerm, Success: false}
	if ae.PrevLogI > n.LastIndex() || n.termAt(ae.PrevLogI) != ae.PrevLogT {
		// Log consistency check failed; leader will decrement and retry.
		return &Message{
			Kind: MsgAppendResp, From: n.id, To: ae.LeaderID,
			Term: n.currentTerm, Append: resp, Deliver: now + timeout,
		}
	}

	// Merge: if an existing entry conflicts with a new one (different term),
	// truncate it and everything after; append entries beyond the current log
	// length. Index is authoritative, not slice position.
	for _, e := range ae.Entries {
		switch {
		case e.Index <= n.LastIndex():
			if n.log[e.Index].Term != e.Term {
				n.log = n.log[:e.Index]
				n.log = append(n.log, e)
			}
			// identical entry already present: skip.
		case e.Index == n.LastIndex()+1:
			n.log = append(n.log, e)
		default:
			// Hole (should not happen given a valid PrevLog match): ignore.
		}
	}
	resp.Success = true
	resp.Ack = n.LastIndex()

	if ae.Commit > n.commitIdx {
		n.commitIdx = min(ae.Commit, n.LastIndex())
	}
	n.applyCommitted()
	return &Message{
		Kind: MsgAppendResp, From: n.id, To: ae.LeaderID,
		Term: n.currentTerm, Append: resp, Deliver: now + timeout,
	}
}

// handleAppendResponse processes an AppendEntries response at the leader.
func (n *Node) handleAppendResponse(m Message, now, timeout int64) []Message {
	ae := m.Append
	if n.role != Leader {
		return nil
	}
	if ae.Term > n.currentTerm {
		n.becomeFollower(ae.Term)
		return nil
	}
	follower := m.From
	if !ae.Success {
		// Back up one entry and immediately retry (deterministic back-off is
		// faster to converge than waiting for the next heartbeat).
		if n.nextIndex[follower] > 1 {
			n.nextIndex[follower]--
		}
		prevI := n.nextIndex[follower] - 1
		req := &AppendEntries{
			Term:     n.currentTerm,
			LeaderID: n.id,
			PrevLogI: prevI,
			PrevLogT: n.termAt(prevI),
			Commit:   n.commitIdx,
		}
		for i := prevI + 1; i <= n.LastIndex(); i++ {
			req.Entries = append(req.Entries, n.log[i])
		}
		return []Message{{
			Kind: MsgAppendReq, From: n.id, To: follower, Term: n.currentTerm,
			Append: req, Deliver: now + timeout,
		}}
	}

	// Success: advance match/next indexes from the follower-acknowledged
	// prefix length stamped on the response. The follower's last index may be
	// shorter than this leader thinks (e.g. after restart); never regress it.
	acked := ae.AckedIndex()
	if acked > n.matchIndex[follower] {
		n.matchIndex[follower] = acked
		n.nextIndex[follower] = acked + 1
	} else if n.nextIndex[follower] > acked+1 {
		n.nextIndex[follower] = acked + 1
	}
	// Even an empty heartbeat ack informs quorum: re-evaluate commit.
	n.advanceCommitIndex()
	n.applyCommitted()
	return nil
}

// advanceCommitIndex implements the majority-commit rule (standard) or the
// one-quorum rule (naive variant).
func (n *Node) advanceCommitIndex() {
	if n.role != Leader {
		return
	}
	// Gather replicated indexes: the leader has everything, followers report
	// via matchIndex.
	matched := make([]int, 0, len(n.peers))
	matched = append(matched, n.LastIndex())
	for _, p := range n.peers {
		if p == n.id {
			continue
		}
		matched = append(matched, n.matchIndex[p])
	}
	sort.Sort(sort.Reverse(sort.IntSlice(matched)))

	if n.variant.CommitByOneQuorum {
		// BUG: any replicated entry (even to one node) is treated as
		// committed, ignoring majority and the current-term rule.
		n.commitIdx = matched[0]
		return
	}
	// Standard Raft: N is committable if a majority replicated it and at
	// least one committable entry is from the leader's current term; then
	// commit through N.
	majority := matched[len(matched)/2]
	for i := majority; i > n.commitIdx; i-- {
		if n.termAt(i) == n.currentTerm {
			n.commitIdx = i
			return
		}
	}
}

// applyCommitted applies newly committed entries to the KV state machine in
// order. Idempotent: rebuilding from the log after restart re-applies entries
// but the map state converges.
func (n *Node) applyCommitted() {
	for n.lastApplied < n.commitIdx && n.lastApplied+1 <= n.LastIndex() {
		n.lastApplied++
		e := n.log[n.lastApplied]
		applyKV(n.kv, e.Command)
	}
}

// ClientCommand appends a new command to the leader's log. Returns false on a
// non-leader.
func (n *Node) ClientCommand(cmd string, now, timeout int64) []Message {
	if n.role != Leader {
		return nil
	}
	e := LogEntry{Term: n.currentTerm, Index: n.LastIndex() + 1, Command: cmd}
	n.log = append(n.log, e)
	n.matchIndex[n.id] = e.Index
	n.nextIndex[n.id] = e.Index + 1
	// Optimistic commit check is not done here; commit follows replication.
	n.advanceCommitIndex()
	n.applyCommitted()
	return n.appendToAll(now, timeout)
}

// noopCommand is the leader-elected marker appended so previous-term entries
// become committable (Raft §8). It is a no-op for the KV state machine.
const noopCommand = "noop"

// --- KV state machine -------------------------------------------------------

// applyKV interprets commands of the form "SET k v" and "DELETE k". Anything
// else is applied as a no-op but still occupies a log slot.
func applyKV(kv map[string]string, cmd string) {
	if len(cmd) >= 4 && cmd[:4] == "SET " {
		rest := cmd[4:]
		for i := 0; i < len(rest); i++ {
			if rest[i] == ' ' {
				k := rest[:i]
				v := rest[i+1:]
				if k != "" {
					kv[k] = v
				}
				return
			}
		}
		return
	}
	if len(cmd) >= 7 && cmd[:7] == "DELETE " {
		k := cmd[7:]
		if k != "" {
			delete(kv, k)
		}
	}
}

// KV returns a copy of the applied state machine.
func (n *Node) KV() map[string]string {
	out := make(map[string]string, len(n.kv))
	for k, v := range n.kv {
		out[k] = v
	}
	return out
}

// ElectionTimeout draws a jittered election timeout in virtual nanoseconds
// from the injected RNG. The jitter span equals the base timeout (i.e. a
// timeout in [base, 2*base)) which spreads simultaneous startup timeouts far
// enough apart relative to the network delay that split votes are essentially
// impossible while keeping a full election under 250ms.
func ElectionTimeout(rng *rand.Rand, baseMs int) int64 {
	if baseMs < 1 {
		baseMs = 1
	}
	jitter := rng.Intn(baseMs)
	return int64(baseMs+jitter) * 1_000_000
}
