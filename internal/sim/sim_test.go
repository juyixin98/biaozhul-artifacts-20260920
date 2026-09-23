package sim

import (
	"encoding/json"
	"reflect"
	"testing"

	"quorumcheck/internal/quorum"
)

func cfg3() *quorum.Config {
	return &quorum.Config{
		Nodes: []quorum.Node{
			{ID: "n1", Weight: 1, Domain: "a"},
			{ID: "n2", Weight: 1, Domain: "b"},
			{ID: "n3", Weight: 1, Domain: "c"},
		},
		ReadThreshold:  2,
		WriteThreshold: 2,
	}
}

// TestDeterminism: identical specs must produce byte-identical JSON output.
func TestDeterminism(t *testing.T) {
	spec := &RunSpec{
		Config:  cfg3(),
		Seed:    7,
		Network: NetParams{DropRate: 0.2, DupRate: 0.3, MaxDelay: 5},
		Trace:   true,
		Ops: []OpSpec{
			{ID: "w1", Type: "write", At: 0, Version: 1, Value: "x"},
			{ID: "w2", Type: "write", At: 1, Version: 2, Value: "y"},
			{ID: "r1", Type: "read", At: 30},
		},
	}
	r1, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	j1, _ := json.Marshal(r1)
	j2, _ := json.Marshal(r2)
	if !reflect.DeepEqual(j1, j2) {
		t.Fatal("same seed produced different results")
	}
}

// TestSafeConfigNoViolation: a safe quorum config with a benign network
// must let a later read observe the completed write.
func TestSafeConfigNoViolation(t *testing.T) {
	spec := &RunSpec{
		Config:  cfg3(),
		Seed:    1,
		Network: NetParams{MaxDelay: 3},
		Ops: []OpSpec{
			{ID: "w1", Type: "write", At: 0, Version: 1, Value: "x"},
			{ID: "r1", Type: "read", At: 30},
		},
	}
	res, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Violations) != 0 {
		t.Fatalf("unexpected violations: %+v", res.Violations)
	}
	var read *OpResult
	for i := range res.Ops {
		if res.Ops[i].ID == "r1" {
			read = &res.Ops[i]
		}
	}
	if read == nil || read.Status != "ok" || read.Value != "x" || read.Version != 1 {
		t.Fatalf("read should observe the write, got %+v", read)
	}
}

// TestUnsafeConfigViolation: 4 weight-1 nodes, R=W=2 (2+2 not > 4). Two
// writes land on disjoint quorums; a read of the stale quorum must be
// flagged as an atomicity violation.
func TestUnsafeConfigViolation(t *testing.T) {
	cfg := &quorum.Config{
		Nodes: []quorum.Node{
			{ID: "n1", Weight: 1}, {ID: "n2", Weight: 1},
			{ID: "n3", Weight: 1}, {ID: "n4", Weight: 1},
		},
		ReadThreshold:  2,
		WriteThreshold: 2,
	}
	spec := &RunSpec{
		Config:  cfg,
		Seed:    1,
		Network: NetParams{},
		Ops: []OpSpec{
			{ID: "w1", Type: "write", At: 0, Version: 1, Value: "a", Targets: []string{"n1", "n2"}},
			{ID: "w2", Type: "write", At: 0, Version: 2, Value: "b", Targets: []string{"n3", "n4"}},
			{ID: "r1", Type: "read", At: 20, Targets: []string{"n1", "n2"}},
		},
	}
	res, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Violations) != 1 {
		t.Fatalf("expected exactly one violation, got %+v", res.Violations)
	}
	v := res.Violations[0]
	if v.ReadID != "r1" || v.MissingWriteID != "w2" || v.ReadVersion != 1 || v.MissingVersion != 2 {
		t.Fatalf("unexpected violation: %+v", v)
	}
}

// TestDropsCauseTimeout: with drop_rate 1 nothing is delivered.
func TestDropsCauseTimeout(t *testing.T) {
	spec := &RunSpec{
		Config:  cfg3(),
		Seed:    3,
		Network: NetParams{DropRate: 1},
		Ops:     []OpSpec{{ID: "w1", Type: "write", At: 0, Version: 1, Value: "x", Timeout: 10}},
	}
	res, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	if res.Ops[0].Status != "timeout" {
		t.Fatalf("expected timeout, got %+v", res.Ops[0])
	}
}

// TestDuplicatesDoNotDoubleCount: duplicate responses must not inflate the
// accumulated weight — one weight-1 node can never form a weight-2 quorum.
func TestDuplicatesDoNotDoubleCount(t *testing.T) {
	cfg := &quorum.Config{
		Nodes:          []quorum.Node{{ID: "n1", Weight: 1}},
		ReadThreshold:  2,
		WriteThreshold: 2,
	}
	spec := &RunSpec{
		Config:  cfg,
		Seed:    5,
		Network: NetParams{DupRate: 1},
		Ops:     []OpSpec{{ID: "w1", Type: "write", At: 0, Version: 1, Value: "x", Timeout: 10}},
	}
	res, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	if res.Ops[0].Status != "timeout" {
		t.Fatalf("duplicate acks must not complete a quorum: %+v", res.Ops[0])
	}
}

// TestDownNodes: a node listed in `down` never answers; with 2 of 3 nodes
// down no weight-2 quorum is possible.
func TestDownNodes(t *testing.T) {
	spec := &RunSpec{
		Config:  cfg3(),
		Seed:    1,
		Down:    []string{"n2", "n3"},
		Network: NetParams{},
		Ops:     []OpSpec{{ID: "w1", Type: "write", At: 0, Version: 1, Value: "x", Timeout: 10}},
	}
	res, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	if res.Ops[0].Status != "timeout" {
		t.Fatalf("expected timeout with two nodes down, got %+v", res.Ops[0])
	}
	if res.Final["n2"].Version != 0 || res.Final["n3"].Version != 0 {
		t.Fatalf("down nodes must stay untouched: %+v", res.Final)
	}
}

// TestReordering: with random delays, a later-issued higher-version write
// still wins on every node it reaches (last-writer-wins by version).
func TestReordering(t *testing.T) {
	spec := &RunSpec{
		Config:  cfg3(),
		Seed:    11,
		Network: NetParams{MaxDelay: 10},
		Ops: []OpSpec{
			{ID: "w1", Type: "write", At: 0, Version: 1, Value: "old"},
			{ID: "w2", Type: "write", At: 1, Version: 2, Value: "new"},
		},
	}
	res, err := Run(spec)
	if err != nil {
		t.Fatal(err)
	}
	for id, st := range res.Final {
		if st.Version != 2 || st.Value != "new" {
			t.Fatalf("node %s: expected version 2, got %+v", id, st)
		}
	}
}

func TestRunSpecValidation(t *testing.T) {
	bad := &RunSpec{
		Config: cfg3(),
		Ops:    []OpSpec{{ID: "w1", Type: "write", At: 0, Version: 0, Value: "x"}},
	}
	if _, err := Run(bad); err == nil {
		t.Fatal("expected error for non-positive write version")
	}
	dup := &RunSpec{
		Config: cfg3(),
		Ops: []OpSpec{
			{ID: "r1", Type: "read", At: 0},
			{ID: "r1", Type: "read", At: 1},
		},
	}
	if _, err := Run(dup); err == nil {
		t.Fatal("expected error for duplicate op id")
	}
	unknown := &RunSpec{
		Config: cfg3(),
		Ops:    []OpSpec{{ID: "r1", Type: "read", At: 0, Targets: []string{"ghost"}}},
	}
	if _, err := Run(unknown); err == nil {
		t.Fatal("expected error for unknown target")
	}
}
