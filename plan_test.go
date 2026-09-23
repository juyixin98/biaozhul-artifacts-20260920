package main

import (
	"encoding/json"
	"math/big"
	"math/rand"
	"sort"
	"testing"
)

var spaceBig = new(big.Int).Lsh(big.NewInt(1), 64)

// intervals expands an arc to half-open integer intervals [lo, hi) over the
// linear [0, 2^64) space.
func arcIntervals(a Arc) [][2]*big.Int {
	start := new(big.Int).SetUint64(a.StartExclusive)
	end := new(big.Int).SetUint64(a.EndInclusive)
	lo := new(big.Int).Add(start, big.NewInt(1))
	hi := new(big.Int).Add(end, big.NewInt(1))
	if !a.Wrap {
		return [][2]*big.Int{{lo, hi}}
	}
	// Whole-circle idiom and ordinary wrap arcs both decompose this way;
	// empty pieces (lo >= space, or hi <= 0) are filtered by the caller's
	// chain check naturally.
	var out [][2]*big.Int
	if lo.Cmp(spaceBig) < 0 {
		out = append(out, [2]*big.Int{new(big.Int).Set(lo), new(big.Int).Set(spaceBig)})
	}
	if hi.Sign() > 0 {
		out = append(out, [2]*big.Int{big.NewInt(0), new(big.Int).Set(hi)})
	}
	return out
}

// assertTilesCircle verifies all arcs (moves+stays) are pairwise disjoint,
// leave no gaps and exactly cover [0, 2^64).
func assertTilesCircle(t *testing.T, p *Plan) {
	t.Helper()
	var ivs [][2]*big.Int
	var owners []string
	for _, m := range p.Moved {
		for _, iv := range arcIntervals(m.Arc) {
			if iv[0].Cmp(iv[1]) >= 0 {
				t.Fatalf("empty/degenerate interval in move %s->%s: %v", m.From, m.To, iv)
			}
			ivs = append(ivs, iv)
			owners = append(owners, m.From+"->"+m.To)
		}
	}
	for _, s := range p.Stayed {
		for _, iv := range arcIntervals(s.Arc) {
			if iv[0].Cmp(iv[1]) >= 0 {
				t.Fatalf("empty/degenerate interval in stay %s: %v", s.Node, iv)
			}
			ivs = append(ivs, iv)
			owners = append(owners, "stay:"+s.Node)
		}
	}
	sort.Slice(ivs, func(i, j int) bool { return ivs[i][0].Cmp(ivs[j][0]) < 0 })
	if len(ivs) == 0 {
		t.Fatal("plan covers no space")
	}
	cursor := big.NewInt(0)
	for i, iv := range ivs {
		if iv[0].Cmp(cursor) != 0 {
			t.Fatalf("gap or overlap at interval %d (%s): want start %s got %s", i, owners[i], cursor, iv[0])
		}
		cursor.Set(iv[1])
	}
	if cursor.Cmp(spaceBig) != 0 {
		t.Fatalf("coverage ends at %s, want %s (not seamless)", cursor, spaceBig)
	}

	// Length totals must agree.
	sumMoved := big.NewInt(0)
	for _, m := range p.Moved {
		sumMoved.Add(sumMoved, m.Arc.Length())
	}
	sumStayed := big.NewInt(0)
	for _, s := range p.Stayed {
		sumStayed.Add(sumStayed, s.Arc.Length())
	}
	total := new(big.Int).Add(sumMoved, sumStayed)
	if total.Cmp(spaceBig) != 0 {
		t.Fatalf("arc lengths sum to %s, want %s", total, spaceBig)
	}
	if p.TotalLength != spaceBig.String() {
		t.Fatalf("total_length = %s", p.TotalLength)
	}
	if p.MovedLength != sumMoved.String() {
		t.Fatalf("moved_length = %s, sum of arcs = %s", p.MovedLength, sumMoved)
	}
}

// containingArc finds the arc (move or stay) containing h.
func containingArc(t *testing.T, p *Plan, h uint64) (from, to string, isMove bool) {
	t.Helper()
	n := 0
	for _, m := range p.Moved {
		if m.Arc.Contains(h) {
			from, to, isMove = m.From, m.To, true
			n++
		}
	}
	for _, s := range p.Stayed {
		if s.Arc.Contains(h) {
			from, to, isMove = s.Node, s.Node, false
			n++
		}
	}
	if n != 1 {
		t.Fatalf("hash %016x matched %d arcs (want exactly 1)", h, n)
	}
	return
}

// assertPlanOwnership verifies the plan against direct ring routing at every
// given probe hash (including boundary positions).
func assertPlanOwnership(t *testing.T, old, nxt *Ring, p *Plan, probes []uint64) {
	t.Helper()
	for _, h := range probes {
		var oldOwner, newOwner string
		if old != nil && !old.Empty() {
			oldOwner, _ = old.RouteHash(h)
		}
		if nxt != nil && !nxt.Empty() {
			newOwner, _ = nxt.RouteHash(h)
		}
		from, to, isMove := containingArc(t, p, h)
		if from != oldOwner || to != newOwner {
			t.Fatalf("h=%016x plan says %q->%q but rings route %q->%q",
				h, from, to, oldOwner, newOwner)
		}
		if (oldOwner != newOwner) != isMove {
			t.Fatalf("h=%016x move flag mismatch", h)
		}
	}
}

// boundaryProbes returns every vnode hash in both rings plus hash+0/+1/-1
// neighbours: the exact endpoints the interval claims depend on.
func boundaryProbes(rs ...*Ring) []uint64 {
	seen := map[uint64]struct{}{}
	add := func(h uint64) {
		seen[h] = struct{}{}
		seen[h+1] = struct{}{}
		if h > 0 {
			seen[h-1] = struct{}{}
		}
	}
	for _, r := range rs {
		if r != nil {
			for _, v := range r.VNodes() {
				add(v.Hash)
			}
		}
	}
	out := make([]uint64, 0, len(seen))
	for h := range seen {
		out = append(out, h)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func TestPlanAddNode_PartitionsAndMatchesRouting(t *testing.T) {
	old, _ := New(letterNodes(5, 1), 64)
	next, _ := New(append(letterNodes(5, 1), Node{ID: "node-f", Weight: 1}), 64)
	p := BuildPlan(1, 2, old, next)

	assertTilesCircle(t, p)
	assertPlanOwnership(t, old, next, p, boundaryProbes(old, next))

	// Random keys: unaffected keys must not appear in moved arcs; moved keys
	// must be exactly those whose owner changed.
	rng := rand.New(rand.NewSource(11))
	probes := make([]uint64, 100000)
	for i := range probes {
		probes[i] = rng.Uint64()
	}
	assertPlanOwnership(t, old, next, p, probes)
}

func TestPlanIdenticalRings_NoMoves(t *testing.T) {
	r, _ := New([]Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 2}}, 64)
	p := BuildPlan(3, 3, r, r)
	assertTilesCircle(t, p)
	if len(p.Moved) != 0 {
		t.Fatalf("identical rings produce %d moves", len(p.Moved))
	}
	if p.MovedLength != "0" || p.MovedFraction != 0 {
		t.Fatalf("identical rings: moved=%s frac=%v", p.MovedLength, p.MovedFraction)
	}
	// Every stay must keep the same node; total unaffected length is 2^64.
	unaffected := big.NewInt(0)
	for _, s := range p.Stayed {
		unaffected.Add(unaffected, s.Arc.Length())
	}
	if unaffected.Cmp(spaceBig) != 0 || len(p.Stayed) == 0 {
		t.Fatalf("identical rings: stayed arcs %d cover %s", len(p.Stayed), unaffected)
	}
}

func TestPlanSingleNodeWholeCircle(t *testing.T) {
	r, _ := New([]Node{{ID: "solo", Weight: 1}}, 8)

	// Empty -> single node: one whole-circle move "" -> solo (the
	// start==end + wrap idiom used by the planner).
	empty, _ := New(nil, 8)
	p := BuildPlan(0, 1, empty, r)
	assertTilesCircle(t, p)
	if len(p.Moved) != 1 || p.Moved[0].From != "" || p.Moved[0].To != "solo" ||
		!p.Moved[0].Arc.Wrap ||
		p.Moved[0].Arc.StartExclusive != p.Moved[0].Arc.EndInclusive ||
		p.Moved[0].Arc.Length().Cmp(spaceBig) != 0 {
		t.Fatalf("expected one whole-circle move into solo, got %+v", p.Moved)
	}

	// Identical single node: one whole-circle stay.
	q := BuildPlan(1, 1, r, r)
	if len(q.Stayed) != 1 || !q.Stayed[0].Arc.Wrap ||
		q.Stayed[0].Arc.StartExclusive != q.Stayed[0].Arc.EndInclusive ||
		q.Stayed[0].Arc.Length().Cmp(spaceBig) != 0 {
		t.Fatalf("expected one whole-circle stay, got %+v", q.Stayed)
	}
}

func TestPlanEmptyToNodesAndBack(t *testing.T) {
	empty, _ := New(nil, 64)
	r, _ := New([]Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}}, 64)

	p := BuildPlan(0, 1, empty, r)
	assertTilesCircle(t, p)
	assertPlanOwnership(t, empty, r, p, boundaryProbes(r))
	rng := rand.New(rand.NewSource(5))
	probes := make([]uint64, 50000)
	for i := range probes {
		probes[i] = rng.Uint64()
	}
	assertPlanOwnership(t, empty, r, p, probes)
	for _, m := range p.Moved {
		if m.From != "" {
			t.Fatalf("initial assignment must move from empty, got %s", m.From)
		}
	}

	// Drain everything: with two prior owners there are as many draining
	// arcs as owner runs, all ending at "", and their lengths sum to 2^64.
	q := BuildPlan(1, 2, r, empty)
	assertTilesCircle(t, q)
	assertPlanOwnership(t, r, empty, q, probes)
	if len(q.Moved) == 0 || len(q.Stayed) != 0 {
		t.Fatalf("draining plan wrong: %d moves %d stays", len(q.Moved), len(q.Stayed))
	}
	drainLen := big.NewInt(0)
	for _, m := range q.Moved {
		if m.To != "" {
			t.Fatalf("draining move must end at empty, got %s", m.To)
		}
		drainLen.Add(drainLen, m.Arc.Length())
	}
	if drainLen.Cmp(spaceBig) != 0 {
		t.Fatalf("draining arcs cover %s, want %s", drainLen, spaceBig)
	}
}

// ownershipLengths independently sums the arc gaps each node owns directly
// from vnode positions (a second, simpler implementation to cross-check the
// planner's flow accounting).
func ownershipLengths(r *Ring) map[string]*big.Int {
	out := map[string]*big.Int{}
	add := func(id string, l *big.Int) {
		cur, ok := out[id]
		if !ok {
			cur = big.NewInt(0)
			out[id] = cur
		}
		cur.Add(cur, l)
	}
	if r == nil || r.Empty() {
		return out
	}
	vs := r.VNodes()
	for i, v := range vs {
		prev := vs[(i-1+len(vs))%len(vs)].Hash
		var l *big.Int
		if i == 0 {
			// wrap gap: (last.hash, first.hash] containing 0
			l = new(big.Int).Add(big.NewInt(1), new(big.Int).SetUint64(v.Hash))
			l.Add(l, new(big.Int).Sub(new(big.Int).SetUint64(mathMaxUint64), new(big.Int).SetUint64(prev)))
		} else {
			l = new(big.Int).Sub(new(big.Int).SetUint64(v.Hash), new(big.Int).SetUint64(prev))
		}
		add(v.NodeID, l)
	}
	return out
}

const mathMaxUint64 = ^uint64(0)

func TestPlanNodeFlows(t *testing.T) {
	old, _ := New([]Node{{ID: "a", Weight: 2}, {ID: "b", Weight: 1}, {ID: "gone", Weight: 1}}, 64)
	nxt, _ := New([]Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 3}, {ID: "fresh", Weight: 2}}, 64)
	p := BuildPlan(1, 2, old, nxt)
	assertTilesCircle(t, p)

	oldLen := ownershipLengths(old)
	newLen := ownershipLengths(nxt)

	flows := map[string]NodeFlow{}
	for _, f := range p.NodeFlows {
		flows[f.Node] = f
	}
	parse := func(s string) *big.Int {
		v, ok := new(big.Int).SetString(s, 10)
		if !ok {
			t.Fatalf("bad length %q", s)
		}
		return v
	}
	all := map[string]struct{}{}
	for id := range oldLen {
		all[id] = struct{}{}
	}
	for id := range newLen {
		all[id] = struct{}{}
	}
	for id := range all {
		f := flows[id]
		// old share == unaffected + outgoing
		if ol, was := oldLen[id]; was {
			got := new(big.Int).Add(parse(f.Unaffected), parse(f.Outgoing))
			if got.Cmp(ol) != 0 {
				t.Errorf("node %s: unaffected+outgoing=%s != old share %s", id, got, ol)
			}
		} else if f.Outgoing != "0" {
			t.Errorf("new node %s has outgoing length", id)
		}
		// new share == unaffected + incoming
		if nl, is := newLen[id]; is {
			got := new(big.Int).Add(parse(f.Unaffected), parse(f.Incoming))
			if got.Cmp(nl) != 0 {
				t.Errorf("node %s: unaffected+incoming=%s != new share %s", id, got, nl)
			}
		} else if f.Incoming != "0" {
			t.Errorf("removed node %s has incoming length", id)
		}
	}

	// Global conservation: total incoming == total outgoing == moved length.
	sumIn, sumOut := big.NewInt(0), big.NewInt(0)
	for _, f := range p.NodeFlows {
		sumIn.Add(sumIn, parse(f.Incoming))
		sumOut.Add(sumOut, parse(f.Outgoing))
	}
	if sumIn.Cmp(parse(p.MovedLength)) != 0 || sumOut.Cmp(parse(p.MovedLength)) != 0 {
		t.Fatalf("flow conservation broken: in=%s out=%s moved=%s", sumIn, sumOut, p.MovedLength)
	}
}

func TestPlanDeterministicAcrossRuns(t *testing.T) {
	cfg1 := []Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "c", Weight: 1}}
	cfg2 := []Node{{ID: "a", Weight: 3}, {ID: "b", Weight: 1}, {ID: "c", Weight: 2}, {ID: "d", Weight: 1}}
	build := func() *Plan {
		r1, _ := New(cfg1, 64)
		r2, _ := New(cfg2, 64)
		return BuildPlan(1, 2, r1, r2)
	}
	b1, _ := json.Marshal(build())
	b2, _ := json.Marshal(build())
	if string(b1) != string(b2) {
		t.Fatal("plan JSON not deterministic across independent builds")
	}
}

func TestPlanFuzzManyConfigurations(t *testing.T) {
	rng := rand.New(rand.NewSource(20260924))
	for iter := 0; iter < 60; iter++ {
		mkNodes := func() []Node {
			n := 1 + rng.Intn(8)
			used := map[string]bool{}
			var ns []Node
			for len(ns) < n {
				id := "n" + itoa(rng.Intn(12))
				if used[id] {
					continue
				}
				used[id] = true
				ns = append(ns, Node{ID: id, Weight: 1 + rng.Intn(4)})
			}
			return ns
		}
		old, err1 := New(mkNodes(), 8+rng.Intn(56))
		nxt, err2 := New(mkNodes(), old.BaseVNodes())
		if err1 != nil || err2 != nil {
			t.Fatal("unexpected construction error")
		}
		p := BuildPlan(iter, iter+1, old, nxt)
		assertTilesCircle(t, p)

		probes := boundaryProbes(old, nxt)
		// Cap probe count to keep the test fast.
		if len(probes) > 4000 {
			probes = probes[:4000]
		}
		for i := 0; i < 5000; i++ {
			probes = append(probes, rng.Uint64())
		}
		assertPlanOwnership(t, old, nxt, p, probes)
	}
}

func TestMovedFractionConsistent(t *testing.T) {
	old, _ := New(letterNodes(4, 1), 128)
	nxt, _ := New(append(letterNodes(4, 1), Node{ID: "node-e", Weight: 1}), 128)
	p := BuildPlan(1, 2, old, nxt)
	moved, _ := new(big.Int).SetString(p.MovedLength, 10)
	want, _ := new(big.Float).Quo(new(big.Float).SetInt(moved), new(big.Float).SetInt(spaceBig)).Float64()
	if d := p.MovedFraction - want; d > 1e-12 || d < -1e-12 {
		t.Fatalf("fraction %v != %v", p.MovedFraction, want)
	}
	if p.MovedFraction <= 0 || p.MovedFraction >= 1 {
		t.Fatalf("fraction out of range: %v", p.MovedFraction)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
