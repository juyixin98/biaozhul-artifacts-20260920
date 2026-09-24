package gc

import (
	"context"
	"errors"
	"reflect"
	"testing"

	"orset/internal/orset"
)

// build constructs a replica from add/remove declarations:
// build([][]string{{"a:t1","a:t2","b:t3"}}, [][]string{{"a:t1"}})
func build(adds, dead [][]string) orset.State {
	s := orset.New("b")
	for _, a := range adds {
		for _, spec := range a {
			e, tag := split(spec)
			must(s.ApplyOp(orset.Op{Type: "add", Element: e, Tags: []string{tag}}))
		}
	}
	for _, d := range dead {
		for _, spec := range d {
			e, tag := split(spec)
			must(s.ApplyOp(orset.Op{Type: "remove", Element: e, Tags: []string{tag}}))
		}
	}
	return s.Snapshot()
}

func split(spec string) (string, string) {
	for i := 0; i < len(spec); i++ {
		if spec[i] == ':' {
			return spec[:i], spec[i+1:]
		}
	}
	return spec, ""
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func TestEligibleIntersection(t *testing.T) {
	// Both replicas know t1 dead. Replica 2 has never seen add t2 at all,
	// and r1 has an extra local-only tombstone t3.
	r1 := build(
		[][]string{{"x:t1", "x:t2", "x:t3"}, {"y:t4"}},
		[][]string{{"x:t1", "x:t3"}, {"y:t4"}},
	)
	r2 := build(
		[][]string{{"x:t1"}, {"y:t4"}},
		[][]string{{"x:t1"}, {"y:t4"}},
	)
	got := Eligible([]orset.State{r1, r2})
	want := map[string][]string{"x": {"t1"}, "y": {"t4"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("eligible = %v, want %v", got, want)
	}
}

func TestEligibleEmpty(t *testing.T) {
	r1 := build([][]string{{"x:t1"}}, [][]string{{"x:t1"}})
	r2 := build(nil, nil)
	if got := Eligible([]orset.State{r1, r2}); got != nil {
		t.Fatalf("newborn replica must block ALL GC, got %v", got)
	}
}

// fakeClient is an in-memory gc.Client, optionally failing.
type fakeClient struct {
	state  orset.State
	failAt string // "state" or "purge"
	purged map[string][]string
	purgeN int
}

func (f *fakeClient) State(context.Context) (orset.State, error) {
	if f.failAt == "state" {
		return orset.State{}, errors.New("simulated unreachable peer")
	}
	return f.state, nil
}

func (f *fakeClient) Purge(_ context.Context, tags map[string][]string) (int, error) {
	if f.failAt == "purge" {
		return 0, errors.New("simulated purge failure")
	}
	s := orset.New("f")
	s.Merge(f.state)
	n := s.GCTags(tags)
	f.purged = tags
	f.purgeN = n
	f.state = s.Snapshot()
	return n, nil
}

func TestCoordinatePurgesEverywhere(t *testing.T) {
	r1 := build([][]string{{"x:t1", "x:t2"}}, [][]string{{"x:t1"}})
	r2 := build([][]string{{"x:t1"}}, [][]string{{"x:t1"}})
	c1, c2 := &fakeClient{state: r1}, &fakeClient{state: r2}

	rep, err := Coordinate(context.Background(),
		[]Client{c1, c2}, []string{"u1", "u2"})
	if err != nil {
		t.Fatal(err)
	}
	if rep.TagCount != 2 {
		t.Fatalf("total purged = %d, want 2 (t1 on each node)", rep.TagCount)
	}
	if !reflect.DeepEqual(rep.Purged, []int{1, 1}) {
		t.Fatalf("per-node purges = %v", rep.Purged)
	}
	// t2 must still exist on r1 (never observed by r2): it is a LIVE add.
	if !hasAddTag(c1.state, "x", "t2") {
		t.Fatal("unobserved tag t2 must not have been purged")
	}
	if hasTag(c1.state, "x", "t1") {
		t.Fatal("t1 should have been purged everywhere")
	}
	if hasTag(c2.state, "x", "t1") {
		t.Fatal("t1 should have been purged everywhere")
	}
	// GC is idempotent: second round finds nothing and purges nothing.
	rep2, err := Coordinate(context.Background(), []Client{c1, c2}, []string{"u1", "u2"})
	if err != nil || rep2.TagCount != 0 {
		t.Fatalf("second GC round: err=%v count=%d", err, rep2.TagCount)
	}
}

func TestCoordinateFailsClosedOnStateFetch(t *testing.T) {
	r1 := build([][]string{{"x:t1"}}, [][]string{{"x:t1"}})
	r2 := build([][]string{{"x:t1"}}, [][]string{{"x:t1"}})
	c1, c2 := &fakeClient{state: r1}, &fakeClient{state: r2, failAt: "state"}

	if _, err := Coordinate(context.Background(), []Client{c1, c2}, []string{"u1", "u2"}); err == nil {
		t.Fatal("expected failure when one peer cannot be surveyed")
	}
	// Nothing must have been purged on the reachable replica.
	if c1.purgeN != 0 || c1.purged != nil {
		t.Fatal("fail-closed violated: purge happened despite incomplete survey")
	}
	if !hasTag(c1.state, "x", "t1") {
		t.Fatal("tombstone was lost during an aborted GC round")
	}
}

func hasTag(st orset.State, e, tag string) bool {
	for _, t := range st.Tombstones[e] {
		if t == tag {
			return true
		}
	}
	return false
}

func hasAddTag(st orset.State, e, tag string) bool {
	for _, t := range st.Add[e] {
		if t == tag {
			return true
		}
	}
	return false
}
