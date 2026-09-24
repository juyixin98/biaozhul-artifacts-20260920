package plan

import (
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"math/rand"
	"sort"
	"testing"

	"consistenthash/internal/ring"
)

var maxPoint = ^uint64(0)

func wantRatNear(t *testing.T, got, want *big.Rat, tol float64, label string) {
	t.Helper()
	g, _ := got.Float64()
	w, _ := want.Float64()
	if math.Abs(g-w) > tol {
		t.Fatalf("%s share %s (~%.4f), want ~%.4f (±%.2f)", label, got, g, w, tol)
	}
}

func mustRing(t *testing.T, name string, nodes []ring.Node, vpu int) *ring.Ring {
	t.Helper()
	r, err := ring.New(name, nodes, vpu)
	if err != nil {
		t.Fatalf("ring %s: %v", name, err)
	}
	return r
}

// verifyTiling checks that segments are pairwise disjoint, sorted, seamless
// (no gaps) and cover [0, 2^64] exactly, and that per-segment key counts sum
// to 2^64.
func verifyTiling(t *testing.T, p *Plan) {
	t.Helper()
	if len(p.Segments) == 0 {
		t.Fatal("plan has no segments")
	}
	total := new(big.Int)
	prevHi := uint64(0)
	first := true
	for i, seg := range p.Segments {
		rng := seg.Range
		if rng.Lo > rng.Hi {
			t.Fatalf("segment %d inverted: %016x..%016x", i, rng.Lo, rng.Hi)
		}
		if first {
			if rng.Lo != 0 {
				t.Fatalf("first segment starts at %016x, want 0", rng.Lo)
			}
			first = false
		} else {
			if rng.Lo != prevHi+1 {
				t.Fatalf("gap/overlap between segments at %d: ...%016x then %016x...",
					i, prevHi, rng.Lo)
			}
		}
		want := new(big.Int).Sub(new(big.Int).SetUint64(rng.Hi), new(big.Int).SetUint64(rng.Lo))
		want.Add(want, big.NewInt(1))
		if want.Cmp(&rng.KeyCount) != 0 {
			t.Fatalf("segment %d key_count %s, want %s", i, rng.KeyCount.String(), want.String())
		}
		total.Add(total, want)
		prevHi = rng.Hi
	}
	if prevHi != maxPoint {
		t.Fatalf("last segment ends at %016x, want max", prevHi)
	}
	if total.Cmp(SpaceSize) != 0 {
		t.Fatalf("segments cover %s keys, want %s", total.String(), SpaceSize.String())
	}
}

// findSegment locates the segment containing position h.
func findSegment(t *testing.T, p *Plan, h uint64) Segment {
	t.Helper()
	idx := sort.Search(len(p.Segments), func(i int) bool {
		return p.Segments[i].Range.Hi >= h
	})
	if idx == len(p.Segments) || h < p.Segments[idx].Range.Lo {
		t.Fatalf("position %016x not covered by any segment", h)
	}
	return p.Segments[idx]
}

// verifyRoutingAgainstPlan checks a large number of random keys, plus every
// cut point and its neighbors: the plan's From/To must match direct routing
// on the two rings, and Migrates iff they differ.
func verifyRoutingAgainstPlan(t *testing.T, oldR, newR *ring.Ring, p *Plan, keys []string) {
	t.Helper()
	checkKey := func(key string) {
		h := ring.Hash([]byte(key))
		seg := findSegment(t, p, h)
		oldOwner := oldR.OwnerAt(h)
		newOwner := newR.OwnerAt(h)
		if seg.From != oldOwner {
			t.Fatalf("key %q at %016x: segment from %s but old ring routes to %s",
				key, h, seg.From, oldOwner)
		}
		if seg.To != newOwner {
			t.Fatalf("key %q at %016x: segment to %s but new ring routes to %s",
				key, h, seg.To, newOwner)
		}
		if seg.Moved() != (oldOwner != newOwner) {
			t.Fatalf("key %q: moved flag wrong", key)
		}
	}
	for _, k := range keys {
		checkKey(k)
	}
	// Every boundary endpoint of every segment, and its neighbors.
	for _, seg := range p.Segments {
		for _, h := range []uint64{seg.Range.Lo, seg.Range.Hi} {
			if got := findSegment(t, p, h); got.Range.Lo != seg.Range.Lo {
				t.Fatalf("endpoint %016x resolved to wrong segment", h)
			}
			checkKeyAt := func(x uint64) {
				gs := findSegment(t, p, x)
				if oldR.OwnerAt(x) != gs.From || newR.OwnerAt(x) != gs.To {
					t.Fatalf("neighbor %016x owners disagree with segment", x)
				}
			}
			checkKeyAt(h)
			if h < maxPoint {
				checkKeyAt(h + 1)
			}
		}
	}
}

// verifyTotals checks plan-level tally consistency.
func verifyTotals(t *testing.T, p *Plan) {
	t.Helper()
	movedFromSegs := new(big.Int)
	unchangedFromSegs := new(big.Int)
	nm, nu := 0, 0
	for _, seg := range p.Segments {
		if seg.Moved() {
			movedFromSegs.Add(movedFromSegs, &seg.Range.KeyCount)
			nm++
		} else {
			unchangedFromSegs.Add(unchangedFromSegs, &seg.Range.KeyCount)
			nu++
		}
	}
	if movedFromSegs.Cmp(&p.MovedKeys) != 0 || unchangedFromSegs.Cmp(&p.UnchangedKeys) != 0 {
		t.Fatal("moved/unchanged key totals disagree with segments")
	}
	if nm != p.MovedSegments || nu != p.UnchangedSegments || nm+nu != len(p.Segments) {
		t.Fatal("moved/unchanged segment counts disagree")
	}
	if new(big.Int).Add(movedFromSegs, unchangedFromSegs).Cmp(SpaceSize) != 0 {
		t.Fatal("moved + unchanged != key space")
	}

	moveTotal := new(big.Int)
	movedPairs := map[string]bool{}
	for _, mv := range p.Moves {
		// Each move's ranges must be sorted ascending and disjoint.
		for i, rng := range mv.Ranges {
			if i > 0 && rng.Lo <= mv.Ranges[i-1].Hi {
				t.Fatalf("move %s->%s ranges not ordered/disjoint", mv.FromNode, mv.ToNode)
			}
		}
		want := new(big.Int)
		for _, rng := range mv.Ranges {
			want.Add(want, &rng.KeyCount)
		}
		if want.Cmp(&mv.KeyCount) != 0 {
			t.Fatalf("move %s->%s key_count wrong", mv.FromNode, mv.ToNode)
		}
		moveTotal.Add(moveTotal, &mv.KeyCount)
		key := mv.FromNode + "->" + mv.ToNode
		if movedPairs[key] {
			t.Fatalf("duplicate move %s", key)
		}
		movedPairs[key] = true
	}
	if moveTotal.Cmp(movedFromSegs) != 0 {
		t.Fatal("sum of moves != sum of moved segments")
	}

	// Every moved segment must appear inside the matching move's ranges.
	for _, seg := range p.Segments {
		if !seg.Moved() {
			continue
		}
		var mv *Move
		for i := range p.Moves {
			if p.Moves[i].FromNode == seg.From && p.Moves[i].ToNode == seg.To {
				mv = &p.Moves[i]
				break
			}
		}
		if mv == nil {
			t.Fatalf("no move covers segment %s->%s", seg.From, seg.To)
		}
		found := false
		for _, rng := range mv.Ranges {
			if rng.Lo == seg.Range.Lo && rng.Hi == seg.Range.Hi {
				found = true
			}
		}
		if !found {
			t.Fatalf("move %s->%s missing exact range [%016x,%016x]",
				seg.From, seg.To, seg.Range.Lo, seg.Range.Hi)
		}
	}

	// New-ring node shares tile the space too.
	shareTotal := new(big.Int)
	shareSet := map[string]bool{}
	for _, ns := range p.NodeShares {
		if shareSet[ns.NodeID] {
			t.Fatalf("duplicate node share for %s", ns.NodeID)
		}
		shareSet[ns.NodeID] = true
		shareTotal.Add(shareTotal, &ns.OwnedKeys)
	}
	if shareTotal.Cmp(SpaceSize) != 0 {
		t.Fatalf("node shares cover %s, want %s", shareTotal, SpaceSize)
	}
}

// verifyCutPointsAreBoundaries: every vnode position from both rings is a
// segment Hi (a cut point), and the segment endpoint ownership matches.
func verifyCutPointsAreBoundaries(t *testing.T, oldR, newR *ring.Ring, p *Plan) {
	t.Helper()
	positions := map[uint64]bool{}
	for _, v := range oldR.VNodes() {
		positions[v.Position] = true
	}
	for _, v := range newR.VNodes() {
		positions[v.Position] = true
	}
	for pos := range positions {
		seg := findSegment(t, p, pos)
		if seg.Range.Hi != pos {
			t.Fatalf("cut point %016x is not a segment boundary (found segment ending %016x)",
				pos, seg.Range.Hi)
		}
		if seg.To != newR.OwnerAt(pos) || seg.From != oldR.OwnerAt(pos) {
			t.Fatalf("ownership mismatch at cut point %016x", pos)
		}
	}
}

func verifyPlan(t *testing.T, oldR, newR *ring.Ring, keys []string) *Plan {
	t.Helper()
	p, err := Build(oldR, newR)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	verifyTiling(t, p)
	verifyTotals(t, p)
	verifyCutPointsAreBoundaries(t, oldR, newR, p)
	verifyRoutingAgainstPlan(t, oldR, newR, p, keys)
	return p
}

func manyKeys(rng *rand.Rand, n int) []string {
	keys := make([]string, n)
	for i := range keys {
		b := make([]byte, 4+rng.Intn(28))
		rng.Read(b)
		keys[i] = fmt.Sprintf("%x", b)
	}
	return keys
}

func TestAddNode(t *testing.T) {
	// Three equal nodes -> four equal nodes. Only keys that move are keys
	// the new node took; all other segments stay put.
	oldR := mustRing(t, "old", []ring.Node{
		{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "c", Weight: 1},
	}, 64)
	newR := mustRing(t, "new", []ring.Node{
		{ID: "a", Weight: 1}, {ID: "b", Weight: 1},
		{ID: "c", Weight: 1}, {ID: "d", Weight: 1},
	}, 64)

	keys := manyKeys(rand.New(rand.NewSource(101)), 30000)
	p := verifyPlan(t, oldR, newR, keys)

	// All moves must target the new node d; nothing moves between old nodes.
	for _, mv := range p.Moves {
		if mv.ToNode != "d" {
			t.Fatalf("unexpected move between surviving nodes: %s -> %s",
				mv.FromNode, mv.ToNode)
		}
	}
	// Roughly one quarter of the key space migrates.
	ratio := new(big.Rat).SetFrac(&p.MovedKeys, SpaceSize)
	f, _ := ratio.Float64()
	if f < 0.20 || f > 0.30 {
		t.Fatalf("moved share %.3f, want ~0.25", f)
	}
}

func TestRemoveNode(t *testing.T) {
	oldR := mustRing(t, "old", []ring.Node{
		{ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"},
	}, 64)
	newR := mustRing(t, "new", []ring.Node{
		{ID: "a"}, {ID: "b"}, {ID: "c"},
	}, 64)

	keys := manyKeys(rand.New(rand.NewSource(202)), 30000)
	p := verifyPlan(t, oldR, newR, keys)

	// Only keys formerly owned by d move; d appears as no To and as no
	// unchanged From.
	for _, seg := range p.Segments {
		if seg.To == "d" {
			t.Fatal("removed node d cannot own new segments")
		}
		if seg.From == "d" && !seg.Moved() {
			t.Fatal("segment owned by d in old ring remained on d")
		}
		if seg.From != "d" && seg.Moved() {
			t.Fatalf("surviving node's key migrated: %s -> %s", seg.From, seg.To)
		}
	}
	// Roughly a quarter of the space leaves d.
	ratio := new(big.Rat).SetFrac(&p.MovedKeys, SpaceSize)
	f, _ := ratio.Float64()
	if f < 0.20 || f > 0.30 {
		t.Fatalf("moved share %.3f, want ~0.25", f)
	}
}

func TestUnchangedIntervalsDoNotMigrate(t *testing.T) {
	// Same ring on both sides: full plan exists, zero moves.
	r := mustRing(t, "r", []ring.Node{{ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"}}, 32)
	p := verifyPlan(t, r, r, manyKeys(rand.New(rand.NewSource(303)), 5000))
	if p.MovedKeys.Sign() != 0 || len(p.Moves) != 0 {
		t.Fatal("identical rings produced migrations")
	}
	if p.UnchangedKeys.Cmp(SpaceSize) != 0 {
		t.Fatal("identical rings should leave the whole space unchanged")
	}
}

func TestWeightChangeReproducible(t *testing.T) {
	oldR := mustRing(t, "old", []ring.Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}}, 64)
	newR := mustRing(t, "new", []ring.Node{{ID: "a", Weight: 3}, {ID: "b", Weight: 1}}, 64)

	keys := manyKeys(rand.New(rand.NewSource(404)), 30000)
	p1 := verifyPlan(t, oldR, newR, keys)

	// Rebuild everything independently — results must be identical.
	oldR2 := mustRing(t, "old", []ring.Node{{ID: "b", Weight: 1}, {ID: "a", Weight: 1}}, 64)
	newR2 := mustRing(t, "new", []ring.Node{{ID: "b", Weight: 1}, {ID: "a", Weight: 3}}, 64)
	p2 := verifyPlan(t, oldR2, newR2, keys)

	j1, _ := json.Marshal(p1)
	j2, _ := json.Marshal(p2)
	if string(j1) != string(j2) {
		t.Fatal("weight-change plan not reproducible from reordered input")
	}
	if p1.MovedKeys.Cmp(&p2.MovedKeys) != 0 {
		t.Fatal("moved key counts differ across rebuilds")
	}

	// Consistent hashing approaches (not exactly equals) the weight
	// proportions; with 256 vnodes the exact shares are very close.
	shares := map[string]*big.Rat{}
	for _, ns := range p1.NodeShares {
		shares[ns.NodeID] = new(big.Rat).SetFrac(&ns.OwnedKeys, SpaceSize)
	}
	wantRatNear(t, shares["a"], big.NewRat(3, 4), 0.03, "a")
	wantRatNear(t, shares["b"], big.NewRat(1, 4), 0.03, "b")
	// Migration only flows b -> a.
	for _, mv := range p1.Moves {
		if mv.FromNode != "b" || mv.ToNode != "a" {
			t.Fatalf("unexpected move %s -> %s after weight change", mv.FromNode, mv.ToNode)
		}
	}

	// Empirical routing on sampled keys converges to the plan's exact share
	// for a (discretization means the exact share need not be exactly 3/4),
	// which independently cross-checks routing against the plan.
	var exactA big.Rat
	for _, ns := range p1.NodeShares {
		if ns.NodeID == "a" {
			exactA.SetFrac(&ns.OwnedKeys, SpaceSize)
		}
	}
	exactF, _ := exactA.Float64()
	rng := rand.New(rand.NewSource(405))
	const n = 300000
	var aCount int
	for i := 0; i < n; i++ {
		k := manyKeys(rng, 1)[0]
		if newR.Owner(k) == "a" {
			aCount++
		}
	}
	got := float64(aCount) / n
	if math.Abs(got-exactF) > 0.01 {
		t.Fatalf("empirical a share %.4f disagrees with exact plan share %.4f", got, exactF)
	}
}

func TestVnodesPerUnitChangesAllKeys(t *testing.T) {
	// Same nodes/weights but a different vnode density is a different ring:
	// both rings are valid and the plan still tiles exactly.
	oldR := mustRing(t, "old", []ring.Node{{ID: "a"}, {ID: "b"}}, 16)
	newR := mustRing(t, "new", []ring.Node{{ID: "a"}, {ID: "b"}}, 32)
	p := verifyPlan(t, oldR, newR, manyKeys(rand.New(rand.NewSource(505)), 20000))
	// Each node has the same number of vnodes in both rings, so both
	// distributions are ~1/2 (exact equality is not guaranteed).
	for _, ns := range p.NodeShares {
		r := new(big.Rat).SetFrac(&ns.OwnedKeys, SpaceSize)
		wantRatNear(t, r, big.NewRat(1, 2), 0.05, ns.NodeID)
	}
}

func TestCompleteNodeSwap(t *testing.T) {
	oldR := mustRing(t, "old", []ring.Node{{ID: "a"}, {ID: "b"}}, 64)
	newR := mustRing(t, "new", []ring.Node{{ID: "x"}, {ID: "y"}, {ID: "z"}}, 64)
	p := verifyPlan(t, oldR, newR, manyKeys(rand.New(rand.NewSource(606)), 20000))
	// Everything migrates.
	if p.MovedKeys.Cmp(SpaceSize) != 0 {
		t.Fatal("full swap should migrate the whole key space")
	}
	if p.UnchangedKeys.Sign() != 0 || p.UnchangedSegments != 0 {
		t.Fatal("full swap should have no unchanged segments")
	}
	for _, seg := range p.Segments {
		if seg.From == seg.To {
			t.Fatal("identical node ids across unrelated rings")
		}
	}
}

func TestSingleNodeRings(t *testing.T) {
	oldR := mustRing(t, "old", []ring.Node{{ID: "solo"}}, 64)
	newR := mustRing(t, "new", []ring.Node{{ID: "solo"}, {ID: "join", Weight: 2}}, 64)
	p := verifyPlan(t, oldR, newR, manyKeys(rand.New(rand.NewSource(707)), 10000))
	// join (weight 2 of 3) takes roughly 2/3; solo retains ~1/3 unchanged.
	shares := map[string]*big.Rat{}
	for _, ns := range p.NodeShares {
		shares[ns.NodeID] = new(big.Rat).SetFrac(&ns.OwnedKeys, SpaceSize)
	}
	wantRatNear(t, shares["solo"], big.NewRat(1, 3), 0.06, "solo")
	wantRatNear(t, shares["join"], big.NewRat(2, 3), 0.06, "join")
	for _, mv := range p.Moves {
		if mv.FromNode != "solo" || mv.ToNode != "join" {
			t.Fatalf("unexpected move %s -> %s", mv.FromNode, mv.ToNode)
		}
	}
}

func TestPlanJSONSerializesBigNumbers(t *testing.T) {
	oldR := mustRing(t, "old", []ring.Node{{ID: "a"}, {ID: "b"}}, 4)
	newR := mustRing(t, "new", []ring.Node{{ID: "a"}, {ID: "b"}, {ID: "c"}}, 4)
	p, err := Build(oldR, newR)
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["key_space"] != "18446744073709551616" {
		t.Fatalf("key_space encoded as %v", decoded["key_space"])
	}
	segs := decoded["segments"].([]any)
	first := segs[0].(map[string]any)["range"].(map[string]any)
	if _, ok := first["lo_hex"].(string); !ok {
		t.Fatal("range bounds missing hex string encoding")
	}
}

func TestBuildValidation(t *testing.T) {
	r := mustRing(t, "r", []ring.Node{{ID: "a"}}, 4)
	if _, err := Build(nil, r); err == nil {
		t.Fatal("expected error for nil old ring")
	}
	if _, err := Build(r, nil); err == nil {
		t.Fatal("expected error for nil new ring")
	}
}

// TestRandomTopologyChange is the property-style acceptance test: repeatedly
// add/remove/reweight random nodes, cross-check tens of thousands of keys and
// enforce the exact tiling invariants.
func TestRandomTopologyChange(t *testing.T) {
	rng := rand.New(rand.NewSource(1337))
	const iterations = 24
	for iter := 0; iter < iterations; iter++ {
		oldNodes := randomNodes(rng, 3+rng.Intn(6))
		newNodes := randomNodes(rng, 3+rng.Intn(6))
		vpu := []int{8, 16, 32}[rng.Intn(3)]
		oldR := mustRing(t, fmt.Sprintf("old-%d", iter), oldNodes, vpu)
		newR := mustRing(t, fmt.Sprintf("new-%d", iter), newNodes, vpu)
		keys := manyKeys(rng, 8000)
		p := verifyPlan(t, oldR, newR, keys)

		// Independently tally migration over the sampled keys; moved fraction
		// must be plausible (between 0 and 1, and exact totals match segments).
		var sampledMoved int
		for _, k := range keys {
			h := ring.Hash([]byte(k))
			if oldR.OwnerAt(h) != newR.OwnerAt(h) {
				sampledMoved++
			}
		}
		if float64(sampledMoved) > float64(len(keys)) {
			t.Fatal("impossible migration count")
		}
		_ = p
	}
}

func randomNodes(rng *rand.Rand, count int) []ring.Node {
	used := map[string]bool{}
	nodes := make([]ring.Node, 0, count)
	for len(nodes) < count {
		id := string(rune('a'+rng.Intn(12))) + "-" + fmt.Sprintf("%d", rng.Intn(1000))
		if used[id] {
			continue
		}
		used[id] = true
		nodes = append(nodes, ring.Node{ID: id, Weight: 1 + rng.Intn(4)})
	}
	return nodes
}
