package sim

import (
	"context"
	"fmt"
	"math/rand"
	"sort"
	"sync"
	"time"

	"raftlab/internal/raft"
)

// StorageFactory builds fresh storages per node ("mem" is used by the
// enumeration; the HTTP server passes file-backed storages).
type StorageFactory func(nodeID int) raft.Storage

func MemoryStorageFactory() StorageFactory {
	return func(int) raft.Storage { return raft.NewMemoryStorage() }
}

// ClusterConfig configures a deterministic simulated cluster.
type ClusterConfig struct {
	Size               int
	ElectionMin        int // ticks
	ElectionMax        int // ticks (exclusive)
	Heartbeat          int // ticks
	Latency            int // ticks, fixed per hop
	Seed               int64
	FixedTimeout       map[int]int // optional per-node pinned election timeout
	BuggyOldTermCommit bool
	Storage            StorageFactory
}

// Cluster is a set of raft nodes plus their shared in-memory network.
type Cluster struct {
	cfg      ClusterConfig
	nodes    map[int]*raft.Node
	fsms     map[int]*raft.KVStateMachine
	storages map[int]raft.Storage
	net      *network
	rngs     map[int]rand.Source
	ids      []int

	mu       sync.Mutex
	autoTick bool // when true, Advance is driven by a real-time ticker
	cancel   func()
}

// SetAutoTick starts or stops a real-time ticker that advances the cluster
// every period (used by the HTTP demo; the fault enumeration leaves it off).
func (c *Cluster) SetAutoTick(on bool, period time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.autoTick == on {
		return
	}
	if !on {
		c.autoTick = false
		if c.cancel != nil {
			c.cancel()
			c.cancel = nil
		}
		return
	}
	c.autoTick = true
	ctx, cancel := context.WithCancel(context.Background())
	c.cancel = cancel
	go func() {
		t := time.NewTicker(period)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				c.mu.Lock()
				if !c.autoTick {
					c.mu.Unlock()
					return
				}
				c.advanceLocked()
				c.mu.Unlock()
			}
		}
	}()
}

// Close stops the auto ticker if running.
func (c *Cluster) Close() { c.SetAutoTick(false, 0) }

// Advance runs one simulation tick: deliver due messages (which may emit
// replies), then tick every live node (which may emit timeouts). Emitted
// messages are handed to the network for the following tick.
func (c *Cluster) Advance() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.advanceLocked()
}

// NewCluster constructs all nodes as followers in term 0.
func NewCluster(cfg ClusterConfig) (*Cluster, error) {
	if cfg.Size < 1 {
		return nil, fmt.Errorf("cluster size must be >= 1")
	}
	if cfg.Storage == nil {
		cfg.Storage = MemoryStorageFactory()
	}
	ids := make([]int, cfg.Size)
	for i := range ids {
		ids[i] = i + 1
	}
	rcfg := raft.Config{
		Nodes:              ids,
		ElectionMin:        cfg.ElectionMin,
		ElectionMax:        cfg.ElectionMax,
		Heartbeat:          cfg.Heartbeat,
		FixedTimeout:       cfg.FixedTimeout,
		BuggyOldTermCommit: cfg.BuggyOldTermCommit,
	}
	c := &Cluster{
		cfg:      cfg,
		nodes:    map[int]*raft.Node{},
		fsms:     map[int]*raft.KVStateMachine{},
		storages: map[int]raft.Storage{},
		net:      newNetwork(cfg.Latency, ids),
		rngs:     map[int]rand.Source{},
		ids:      ids,
	}
	for _, id := range ids {
		// Each node gets an independent deterministic RNG seeded from the
		// cluster seed, so two clusters with the same seed behave identically
		// and a restart continues with a reproducible stream.
		src := rand.NewSource(cfg.Seed + int64(id)*1009)
		c.rngs[id] = src
		st := cfg.Storage(id)
		fsm := raft.NewKVStateMachine()
		n, err := raft.NewNode(id, rcfg, rand.New(src), st, fsm)
		if err != nil {
			return nil, err
		}
		c.nodes[id] = n
		c.fsms[id] = fsm
		c.storages[id] = st
	}
	return c, nil
}

func (c *Cluster) advanceLocked() {
	c.net.tick++

	for _, m := range c.net.due(c.net.tick) {
		c.deliver(m)
	}
	for _, id := range c.ids {
		n := c.nodes[id]
		if !n.Alive() {
			continue
		}
		for _, out := range n.Tick() {
			c.route(out)
		}
	}
}

// IDs returns sorted member IDs.
func (c *Cluster) IDs() []int { return append([]int(nil), c.ids...) }

// Run advances n ticks.
func (c *Cluster) Run(n int) {
	for i := 0; i < n; i++ {
		c.Advance()
	}
}

// deliver hands one message to its recipient if reachable; otherwise it is
// quarantined as a stale message.
func (c *Cluster) deliver(m raft.Message) {
	dst := c.nodes[m.To]
	if dst == nil {
		return
	}
	if !dst.Alive() {
		// Target powered off: the packet lingers in the network and can be
		// replayed later (after a restart) via ReleaseStale.
		c.net.quarantine(m)
		return
	}
	if !c.net.connected(m.From, m.To) {
		c.net.quarantine(m)
		return
	}
	outs := dst.Step(m)
	for _, o := range outs {
		c.route(o)
	}
}

func (c *Cluster) route(m raft.Message) {
	if c.nodes[m.From] == nil || c.nodes[m.To] == nil {
		return
	}
	c.net.send(m)
}

// ---- fault actions --------------------------------------------------------

// Stop powers a node off. Messages already queued for it stay queued; if they
// cannot be delivered they get quarantined.
func (c *Cluster) Stop(id int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if n := c.nodes[id]; n != nil {
		n.Stop()
	}
}

// Start restarts a stopped node, reloading persistent state from storage and
// resetting its volatile state.
func (c *Cluster) Start(id int) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := c.nodes[id]
	if n == nil {
		return fmt.Errorf("unknown node %d", id)
	}
	c.fsms[id].Reset()
	return n.Restart()
}

// Partition isolates a node into its own group.
func (c *Cluster) Partition(id int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.net.setGroup(id, fmt.Sprintf("P%d", id))
}

// SetGroup assigns a node to a named partition group (same group = connected).
func (c *Cluster) SetGroup(id int, group string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.net.setGroup(id, group)
}

// Heal restores full connectivity.
func (c *Cluster) Heal() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.net.heal()
}

// Propose submits a command through the given node. Returns false if that
// node is not currently leader (the command is dropped, as in Raft).
func (c *Cluster) Propose(id int, command string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := c.nodes[id]
	if n == nil || !n.Alive() {
		return false
	}
	msgs, ok := n.Propose(command)
	if !ok {
		return false
	}
	for _, m := range msgs {
		c.route(m)
	}
	return true
}

// ProposeLeader finds the current leader (first node claiming leadership with
// the max term) and proposes through it. Returns whether a leader accepted.
func (c *Cluster) ProposeLeader(command string) (int, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	leader := c.leaderIDLocked()
	if leader < 0 {
		return -1, false
	}
	n := c.nodes[leader]
	msgs, ok := n.Propose(command)
	if !ok {
		return leader, false
	}
	for _, m := range msgs {
		c.route(m)
	}
	return leader, true
}

// ReleaseStale re-delivers all quarantined (old/delayed) messages.
func (c *Cluster) ReleaseStale() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	released := c.net.releaseStale()
	// Deliver immediately at the current tick so the action's effect is
	// observable without an extra Advance.
	for _, m := range c.net.due(c.net.tick) {
		c.deliver(m)
	}
	return released
}

// DropStale discards all quarantined messages.
func (c *Cluster) DropStale() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.net.dropStale()
}

// StaleCount reports the number of quarantined messages.
func (c *Cluster) StaleCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.net.staleCount()
}

// Tick returns the current simulation tick.
func (c *Cluster) Tick() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.net.tick
}

// ---- introspection --------------------------------------------------------

// LeaderID returns the id of the node with leadership in the highest current
// term among live nodes, or -1 if there is no leader. Two live leaders in the
// same term is exactly what the checker looks for.
func (c *Cluster) LeaderID() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.leaderIDLocked()
}

func (c *Cluster) leaderIDLocked() int {
	term, leader := -1, -1
	for _, id := range c.ids {
		n := c.nodes[id]
		if n.Alive() && n.Role() == raft.Leader && n.Term() >= term {
			term, leader = n.Term(), id
		}
	}
	return leader
}

// Leader returns (id, term) of the max-term live leader, or (-1,-1,false).
func (c *Cluster) Leader() (id, term int, ok bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	id, t := -1, -1
	for _, nid := range c.ids {
		n := c.nodes[nid]
		if n.Alive() && n.Role() == raft.Leader && n.Term() > t {
			id, t = nid, n.Term()
		}
	}
	if id < 0 {
		return -1, -1, false
	}
	return id, t, true
}

// LeadersByTerm returns live leaders grouped by term: term -> node ids.
func (c *Cluster) LeadersByTerm() map[int][]int {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[int][]int{}
	for _, id := range c.ids {
		n := c.nodes[id]
		if n.Alive() && n.Role() == raft.Leader {
			out[n.Term()] = append(out[n.Term()], id)
		}
	}
	return out
}

// Node returns the raft node snapshot.
func (c *Cluster) Node(id int) raft.Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.nodes[id].Snapshot()
}

// Nodes returns all snapshots ordered by id.
func (c *Cluster) Nodes() []raft.Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]raft.Snapshot, 0, len(c.ids))
	for _, id := range c.ids {
		out = append(out, c.nodes[id].Snapshot())
	}
	return out
}

// KV returns the state machine map for a node.
func (c *Cluster) KV(id int) map[string]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.fsms[id].Snapshot()
}

// CommittedHistory returns every node's committed prefix, including stopped
// nodes (their on-disk prefix still counts for the commit invariant).
func (c *Cluster) CommittedHistory() map[int][]raft.Entry {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[int][]raft.Entry{}
	for _, id := range c.ids {
		s := c.nodes[id].Snapshot()
		out[id] = s.Committed
	}
	return out
}

// SortedNodeIDs is a small convenience for callers.
func SortedNodeIDs(ids []int) []int {
	out := append([]int(nil), ids...)
	sort.Ints(out)
	return out
}
