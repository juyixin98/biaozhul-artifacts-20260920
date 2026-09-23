package register

import (
	"encoding/json"
	"errors"
	"testing"

	"vcconflict/internal/vclock"
)

func ids(vs []Version) []string {
	out := make([]string, len(vs))
	for i, v := range vs {
		out[i] = v.ID
	}
	return out
}

func expectIDs(t *testing.T, got []Version, want ...string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("versions=%v want %v", ids(got), want)
	}
	for i, w := range want {
		if got[i].ID != w {
			t.Fatalf("versions=%v want %v", ids(got), want)
		}
	}
}

func raw(s string) json.RawMessage { return json.RawMessage(s) }

func rejectReason(t *testing.T, err error) string {
	t.Helper()
	var rej *RejectError
	if !errors.As(err, &rej) {
		t.Fatalf("expected RejectError, got %v", err)
	}
	return rej.Reason
}

// The acceptance backbone: two independent offline writes become siblings
// on gossip; a merge write with the FULL context replaces both; a write
// naming only one sibling is refused, so neither concurrent version can be
// silently lost.
func TestOfflineWritesThenMerge(t *testing.T) {
	n1, n2 := NewStore("n1"), NewStore("n2")

	if _, err := n1.Write("k", raw(`"A"`), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := n2.Write("k", raw(`"B"`), nil); err != nil {
		t.Fatal(err)
	}

	n2.MergeSnapshot(n1.Snapshot())
	expectIDs(t, n2.Read("k"), "n1#1", "n2#1")

	// Partial context (one of two siblings) -> rejected, store unchanged.
	_, err := n2.Write("k", raw(`"clobber"`), []string{"n2#1"})
	if rejectReason(t, err) != "stale_context" {
		t.Fatalf("want stale_context, got %v", err)
	}
	var rej *RejectError
	errors.As(err, &rej)
	if rej.StaleUnseen == nil || rej.StaleUnseen.ID != "n1#1" {
		t.Fatalf("endangered version=%+v want n1#1", rej.StaleUnseen)
	}
	expectIDs(t, n2.Read("k"), "n1#1", "n2#1")

	// Full context -> one descendant replaces both.
	rep, err := n2.Write("k", raw(`"AB"`), []string{"n1#1", "n2#1"})
	if err != nil {
		t.Fatal(err)
	}
	if rep.NewVersion.ID != "n2#2" {
		t.Fatalf("new id=%s want n2#2", rep.NewVersion.ID)
	}
	if want := []string{"n1#1", "n2#1"}; len(rep.Covered) != 2 ||
		rep.Covered[0] != want[0] || rep.Covered[1] != want[1] {
		t.Fatalf("covered=%v want %v", rep.Covered, want)
	}
	expectIDs(t, n2.Read("k"), "n2#2")
	if vclock.Compare(rep.NewVersion.Clock, vclock.Clock{"n1": 1, "n2": 2}) != vclock.EqualRel {
		t.Fatalf("clock=%v", rep.NewVersion.Clock)
	}

	// Resolution propagates symmetrically; n1 prunes its ancestor.
	r := n1.MergeSnapshot(n2.Snapshot())["k"]
	if len(r.Pruned) != 1 || r.Pruned[0] != "n1#1" {
		t.Fatalf("merge pruned=%v want [n1#1]", r.Pruned)
	}
	expectIDs(t, n1.Read("k"), "n2#2")
}

// A brand-new key accepts an empty context; a second empty-context write
// is a stale overwrite and is refused.
func TestEmptyContextRules(t *testing.T) {
	n := NewStore("n1")
	if _, err := n.Write("k", raw(`1`), []string{}); err != nil {
		t.Fatal(err)
	}
	_, err := n.Write("k", raw(`2`), []string{})
	if rejectReason(t, err) != "stale_context" {
		t.Fatalf("blind overwrite want stale_context, got %v", err)
	}
	expectIDs(t, n.Read("k"), "n1#1")
}

// A context naming an unknown version is rejected and changes nothing.
func TestUnknownContextRejected(t *testing.T) {
	n := NewStore("n1")
	if _, err := n.Write("k", raw(`"a"`), nil); err != nil {
		t.Fatal(err)
	}
	_, err := n.Write("k", raw(`"b"`), []string{"n1#1", "ghost#7"})
	var rej *RejectError
	if !errors.As(err, &rej) || rej.Reason != "unknown_context" {
		t.Fatalf("want unknown_context, got %v", err)
	}
	if len(rej.Missing) != 1 || rej.Missing[0] != "ghost#7" {
		t.Fatalf("missing=%v", rej.Missing)
	}
	expectIDs(t, n.Read("k"), "n1#1")
}

// Invalid JSON payloads are rejected without touching the store.
func TestInvalidValueRejected(t *testing.T) {
	n := NewStore("n1")
	if _, err := n.Write("k", raw(`not json`), nil); err == nil {
		t.Fatal("expected error for invalid JSON")
	}
}

// Network merge semantics: replay is idempotent; a dominated incoming
// version is pruned; merge order never changes the final maximal set.
func TestMergeIdempotentAndOrderIndependent(t *testing.T) {
	n1, n2 := NewStore("n1"), NewStore("n2")
	_, _ = n1.Write("k", raw(`"1"`), nil)
	_, _ = n2.Write("k", raw(`"2"`), nil)

	s1 := n1.Snapshot()
	r1 := n2.MergeSnapshot(s1)["k"]
	if len(r1.Added) != 1 || len(r1.Duplicate) != 0 || len(r1.Pruned) != 0 {
		t.Fatalf("first merge=%+v", r1)
	}
	// Replay the identical snapshot: every entry is a duplicate.
	r2 := n2.MergeSnapshot(s1)["k"]
	if len(r2.Duplicate) != 1 || len(r2.Added) != 0 || len(r2.Pruned) != 0 {
		t.Fatalf("replay merge=%+v", r2)
	}
	expectIDs(t, n2.Read("k"), "n1#1", "n2#1")

	// Full-context resolution pruned n1#1 locally. A late snapshot
	// carrying the old ancestor (network reorder) presents an ID n2 no
	// longer holds: it is genuinely "new", but dominated by the local
	// descendant -> pruned on arrival, never resurrected.
	if _, err := n2.Write("k", raw(`"1+2"`), []string{"n1#1", "n2#1"}); err != nil {
		t.Fatal(err)
	}
	expectIDs(t, n2.Read("k"), "n2#2")
	r3 := n2.MergeSnapshot(s1)["k"]
	if len(r3.PrunedIncoming) != 1 || len(r3.Added) != 0 || len(r3.Pruned) != 0 {
		t.Fatalf("late ancestor delivery=%+v", r3)
	}
	expectIDs(t, n2.Read("k"), "n2#2")

	// Replaying n2#2 itself (the actual duplicated packet) is a pure
	// no-op duplicate.
	cur := n2.Snapshot()
	r3b := n2.MergeSnapshot(cur)["k"]
	if len(r3b.Duplicate) != 1 || len(r3b.Added) != 0 {
		t.Fatalf("current-snapshot replay=%+v", r3b)
	}

	// The same stale snapshot arriving at a node that learned only the
	// descendant behaves identically: stale version pruned on arrival.
	n3 := NewStore("n3")
	n3.MergeSnapshot(n2.Snapshot()) // n3 learns n2#2 directly
	expectIDs(t, n3.Read("k"), "n2#2")
	r4 := n3.MergeSnapshot(s1)["k"]
	if len(r4.PrunedIncoming) != 1 || len(r4.Added) != 0 {
		t.Fatalf("stale-at-new-node=%+v", r4)
	}
	expectIDs(t, n3.Read("k"), "n2#2")

	// Symmetric convergence.
	n1.MergeSnapshot(n2.Snapshot())
	expectIDs(t, n1.Read("k"), "n2#2")
}

// Duplicate entries inside one snapshot batch are also idempotent.
func TestDuplicateWithinSnapshot(t *testing.T) {
	n1, n2 := NewStore("n1"), NewStore("n2")
	_, _ = n1.Write("k", raw(`1`), nil)
	v := n1.Read("k")[0]
	rep := n2.MergeSnapshot(Snapshot{Versions: map[string][]Version{
		"k": {v, v, v},
	}})["k"]
	if len(rep.Added) != 1 || len(rep.Duplicate) != 2 {
		t.Fatalf("batch=%+v", rep)
	}
	expectIDs(t, n2.Read("k"), "n1#1")
}

// Gossip union keeps all concurrent branches until a client resolves.
func TestThreeWayForkThenResolution(t *testing.T) {
	nodes := []*Store{NewStore("n1"), NewStore("n2"), NewStore("n3")}
	vals := []string{`"A"`, `"B"`, `"C"`}
	for i, n := range nodes {
		if _, err := n.Write("k", raw(vals[i]), nil); err != nil {
			t.Fatal(err)
		}
	}
	// Full pairwise gossip: every node ends with the three siblings.
	for _, a := range nodes {
		for _, b := range nodes {
			a.MergeSnapshot(b.Snapshot())
		}
	}
	for _, n := range nodes {
		expectIDs(t, n.Read("k"), "n1#1", "n2#1", "n3#1")
	}
	// One node resolves with the complete three-way context.
	rep, err := nodes[0].Write("k", raw(`"all"`), []string{"n1#1", "n2#1", "n3#1"})
	if err != nil {
		t.Fatal(err)
	}
	expectIDs(t, nodes[0].Read("k"), rep.NewVersion.ID)
	// Resolution propagates and collapses siblings everywhere.
	for _, b := range nodes[1:] {
		b.MergeSnapshot(nodes[0].Snapshot())
		expectIDs(t, b.Read("k"), rep.NewVersion.ID)
	}
	nodes[0].MergeSnapshot(nodes[1].Snapshot())
	expectIDs(t, nodes[0].Read("k"), rep.NewVersion.ID)
}

// Snapshot copies are independent of later store mutation.
func TestSnapshotIsolation(t *testing.T) {
	n1, n2 := NewStore("n1"), NewStore("n2")
	_, _ = n1.Write("k", raw(`"a"`), nil)
	snap := n1.Snapshot()
	_, _ = n1.Write("k", raw(`"b"`), []string{"n1#1"})
	n2.MergeSnapshot(snap)
	expectIDs(t, n2.Read("k"), "n1#1")
	if got := snap.Versions["k"][0].Value; string(got) != `"a"` {
		t.Fatalf("snapshot value mutated: %s", got)
	}
}

// Independent keys never interfere; self counters are store-wide and
// monotonic.
func TestMultipleKeysAndCounters(t *testing.T) {
	n := NewStore("n1")
	_, _ = n.Write("a", raw(`1`), nil)              // n1#1
	_, _ = n.Write("b", raw(`1`), nil)              // n1#2
	_, _ = n.Write("a", raw(`2`), []string{"n1#1"}) // n1#3
	expectIDs(t, n.Read("a"), "n1#3")
	expectIDs(t, n.Read("b"), "n1#2")
	if c := n.Read("a")[0].Clock["n1"]; c != 3 {
		t.Fatalf("counter=%d want 3", c)
	}
}

// Context may list IDs in any order and repeat itself.
func TestContextOrderAndDupes(t *testing.T) {
	n1, n2 := NewStore("n1"), NewStore("n2")
	_, _ = n1.Write("k", raw(`1`), nil)
	_, _ = n2.Write("k", raw(`2`), nil)
	n2.MergeSnapshot(n1.Snapshot())
	rep, err := n2.Write("k", raw(`3`), []string{"n2#1", "n1#1", "n1#1"})
	if err != nil {
		t.Fatal(err)
	}
	expectIDs(t, n2.Read("k"), rep.NewVersion.ID)
}
