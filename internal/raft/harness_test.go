package raft

import (
	"math/rand"
	"testing"
)

// testHarness wires nodes with in-memory storages and a direct in-memory
// network, mirroring sim.Cluster but kept local so raft tests have no
// cross-package dependency.
type testHarness struct {
	t       *testing.T
	nodes   map[int]*Node
	fsms    map[int]*KVStateMachine
	pending []Message
	groups  map[int]string
	tick    int
}

func buildTestCluster(t *testing.T, cfg Config, seed int64) ([]*Node, map[int]Storage, map[int]*KVStateMachine) {
	t.Helper()
	nodes := make([]*Node, len(cfg.Nodes))
	storages := map[int]Storage{}
	fsms := map[int]*KVStateMachine{}
	for i, id := range cfg.Nodes {
		src := rand.NewSource(seed + int64(id)*131)
		st := NewMemoryStorage()
		fsm := NewKVStateMachine()
		n, err := NewNode(id, cfg, rand.New(src), st, fsm)
		if err != nil {
			t.Fatal(err)
		}
		nodes[i] = n
		storages[id] = st
		fsms[id] = fsm
	}
	return nodes, storages, fsms
}

func newTestHarness(nodes []*Node, fsms map[int]*KVStateMachine) *testHarness {
	h := &testHarness{
		nodes:  map[int]*Node{},
		fsms:   fsms,
		groups: map[int]string{},
	}
	for _, n := range nodes {
		h.nodes[n.id] = n
		h.groups[n.id] = "A"
	}
	return h
}

func (h *testHarness) send(msgs ...Message) {
	h.pending = append(h.pending, msgs...)
}

func (h *testHarness) deliverDue() {
	due := h.pending
	h.pending = nil
	for _, m := range due {
		dst := h.nodes[m.To]
		if dst == nil || !dst.Alive() || h.groups[m.To] != h.groups[m.From] {
			continue // sim drops quarantined packets here
		}
		outs := dst.Step(m)
		h.pending = append(h.pending, outs...)
	}
}

func (h *testHarness) run(ticks int) {
	for i := 0; i < ticks; i++ {
		h.tick++
		h.deliverDue()
		for id := 1; id <= len(h.nodes); id++ {
			if n := h.nodes[id]; n != nil && n.Alive() {
				h.pending = append(h.pending, n.Tick()...)
			}
		}
	}
}

func (h *testHarness) leader() *Node {
	term, best := -1, (*Node)(nil)
	for _, n := range h.nodes {
		if n.Alive() && n.Role() == Leader && n.Term() > term {
			term, best = n.Term(), n
		}
	}
	return best
}

func (h *testHarness) partition(id int) { h.groups[id] = "P" }

func newSingle(t *testing.T, id int, cfg Config) *Node {
	n, err := NewNode(id, cfg, rand.New(rand.NewSource(1)), NewMemoryStorage(), NewKVStateMachine())
	if err != nil {
		t.Fatal(err)
	}
	return n
}
