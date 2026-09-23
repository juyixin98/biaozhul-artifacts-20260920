package sim

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"vcconflict/internal/register"
	"vcconflict/internal/vclock"
)

func loadExample(t *testing.T, name string) *Scenario {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "examples", name))
	if err != nil {
		t.Fatal(err)
	}
	var sc Scenario
	if err := json.Unmarshal(raw, &sc); err != nil {
		t.Fatal(err)
	}
	return &sc
}

// Run an example and return its result.
func runExample(t *testing.T, name string) *Result {
	t.Helper()
	res, err := Run(loadExample(t, name))
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func kinds(r *Result) []string {
	out := make([]string, len(r.Trace))
	for i, e := range r.Trace {
		out[i] = e.Kind
	}
	return out
}

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}

func countKinds(r *Result) map[string]int {
	m := map[string]int{}
	for _, e := range r.Trace {
		m[e.Kind]++
	}
	return m
}

// 01: offline writes diverge, a partial-context overwrite is rejected,
// the full-context merge resolves, and gossip converges every node.
func TestExampleOfflineWrites(t *testing.T) {
	r := runExample(t, "01-offline-writes.json")
	if r.Stats.WritesRejected != 1 {
		t.Fatalf("rejections=%d want 1", r.Stats.WritesRejected)
	}
	if r.Stats.WritesAccepted != 3 {
		t.Fatalf("accepted=%d want 3", r.Stats.WritesAccepted)
	}
	ks := kinds(r)
	if !contains(ks, "write_rejected") {
		t.Fatal("expected a write_rejected trace entry")
	}
	for _, rej := range r.Trace {
		if rej.Kind == "write_rejected" {
			if rej.Reason != "stale_context" {
				t.Fatalf("reason=%s want stale_context", rej.Reason)
			}
			if rej.NewVersion == nil || rej.NewVersion.ID != "n1#1" {
				t.Fatalf("rejected entry should name the omitted sibling, got %+v", rej.NewVersion)
			}
		}
	}
	// Every node converges to the single resolved descendant n2#2.
	for _, ns := range r.FinalState {
		vs := ns.Keys["profile"]
		if len(vs) != 1 || vs[0].ID != "n2#2" {
			t.Fatalf("node %s final=%v want [n2#2]", ns.Node, idsOf(vs))
		}
	}
}

// 02: guaranteed duplication; replay must be idempotent.
func TestExampleDuplicates(t *testing.T) {
	r := runExample(t, "02-duplicate-delivery.json")
	if r.Stats.Duplicates != 2 {
		t.Fatalf("duplicates=%d want 2", r.Stats.Duplicates)
	}
	if r.Stats.Deliveries != 4 {
		t.Fatalf("deliveries=%d want 4", r.Stats.Deliveries)
	}
	for _, ns := range r.FinalState {
		if ns.Node != "n2" {
			continue
		}
		vs := ns.Keys["k"]
		if len(vs) != 1 || vs[0].ID != "n1#2" {
			t.Fatalf("n2 final=%v want [n1#2]", idsOf(vs))
		}
	}
	// The second copy of each snapshot finds the payload already present
	// (first send) or dominated (second send) and never inflates state.
	noOpDeliveries := 0
	for _, e := range r.Trace {
		if e.Kind != "deliver" {
			continue
		}
		d := e.Merge["k"]
		if len(d.Added) == 0 {
			noOpDeliveries++
		}
	}
	if noOpDeliveries != 2 {
		t.Fatalf("no-op duplicate deliveries=%d want 2", noOpDeliveries)
	}
}

// 03: three rejected client writes (blind overwrite, partial context,
// unknown context version) and one accepted full-context resolution.
func TestExampleStaleContext(t *testing.T) {
	r := runExample(t, "03-stale-context.json")
	reasons := map[string]int{}
	for _, e := range r.Trace {
		if e.Kind == "write_rejected" {
			reasons[e.Reason]++
		}
	}
	if reasons["stale_context"] != 2 || reasons["unknown_context"] != 1 {
		t.Fatalf("rejection reasons=%v want stale_context=2 unknown_context=1", reasons)
	}
	if r.Stats.WritesAccepted != 3 || r.Stats.WritesRejected != 3 {
		t.Fatalf("stats=%+v want accepted=3 rejected=3", r.Stats)
	}
	// Rejected writes must not change state: the read at t=15 on n2
	// shows exactly the resolved single descendant.
	for _, ns := range r.FinalState {
		vs := ns.Keys["doc"]
		if len(vs) != 1 || vs[0].ID != "n2#2" {
			t.Fatalf("node %s final=%v want [n2#2]", ns.Node, idsOf(vs))
		}
	}
	// The unknown-context rejection must quote the offending ID and leave
	// no partial new version behind.
	for _, e := range r.Trace {
		if e.Kind == "write_rejected" && e.Reason == "unknown_context" {
			if len(e.Missing) != 1 || e.Missing[0] != "n9#9" {
				t.Fatalf("missing=%v want [n9#9]", e.Missing)
			}
			if e.NewVersion != nil {
				t.Fatalf("unknown_context rejection must not carry a new version")
			}
		}
	}
}

// 04: reorder — new snapshot arrives first, old snapshot second; the old
// incoming version is pruned and the register stays at the new version.
func TestExampleReorder(t *testing.T) {
	r := runExample(t, "04-reorder.json")
	var firstReachedID string
	var secondPruned []string
	deliveries := 0
	for _, e := range r.Trace {
		if e.Kind != "deliver" {
			continue
		}
		deliveries++
		d := e.Merge["k"]
		if deliveries == 1 {
			if len(d.Added) != 1 || d.Added[0] != "n1#2" {
				t.Fatalf("first delivery should add n1#2, got %+v", d)
			}
			firstReachedID = "n1#2"
		}
		if deliveries == 2 {
			if len(d.PrunedIncoming) != 1 || d.PrunedIncoming[0] != "n1#1" {
				t.Fatalf("late old delivery should prune n1#1, got %+v", d)
			}
			secondPruned = d.PrunedIncoming
		}
	}
	if firstReachedID != "n1#2" || len(secondPruned) != 1 {
		t.Fatalf("reorder not observed correctly (deliveries=%d)", deliveries)
	}
	for _, ns := range r.FinalState {
		if ns.Node == "n2" {
			vs := ns.Keys["k"]
			if len(vs) != 1 || vs[0].ID != "n1#2" {
				t.Fatalf("n2 final=%v want [n1#2]", idsOf(vs))
			}
		}
	}
}

// 05: total drop during partition keeps nodes diverged; repair converges.
func TestExampleDropThenRepair(t *testing.T) {
	r := runExample(t, "05-drop-then-repair.json")
	if r.Stats.Drops != 2 {
		t.Fatalf("drops=%d want 2", r.Stats.Drops)
	}
	// Mid-run read at t=10 on n2 sees only its own offline branch.
	for _, e := range r.Trace {
		if e.Kind == "read" && e.At == 10 {
			if len(e.Siblings) != 1 || e.Siblings[0].ID != "n2#1" {
				t.Fatalf("partitioned read=%v want [n2#1]", idsOf(e.Siblings))
			}
		}
	}
	for _, ns := range r.FinalState {
		vs := ns.Keys["cfg"]
		if len(vs) != 2 {
			t.Fatalf("node %s final=%v want two concurrent siblings", ns.Node, idsOf(vs))
		}
		if idsOf(vs)[0] != "n1#1" || idsOf(vs)[1] != "n2#1" {
			t.Fatalf("node %s final=%v want [n1#1 n2#1]", ns.Node, idsOf(vs))
		}
	}
}

// Determinism: the same seeded scenario run twice produces byte-identical
// JSON output.
func TestDeterministicAcrossRuns(t *testing.T) {
	out1, err := runJSONExample("06-chaos-seeded.json")
	if err != nil {
		t.Fatal(err)
	}
	out2, err := runJSONExample("06-chaos-seeded.json")
	if err != nil {
		t.Fatal(err)
	}
	if string(out1) != string(out2) {
		t.Fatal("seeded scenario is not deterministic")
	}
	// Different seeds should plausibly differ (defensive: run many).
	differ := false
	raw, _ := os.ReadFile(filepath.Join("..", "..", "examples", "06-chaos-seeded.json"))
	for seed := int64(1); seed < 30; seed++ {
		s := int64Seed(t, raw, seed)
		o1, err := RunJSON(mustJSON(s))
		if err != nil {
			t.Fatal(err)
		}
		s2 := int64Seed(t, raw, seed+1000)
		o2, err := RunJSON(mustJSON(s2))
		if err != nil {
			t.Fatal(err)
		}
		if string(o1) != string(o2) {
			differ = true
			break
		}
	}
	if !differ {
		t.Fatal("network RNG appears insensitive to seed")
	}
}

// Convergence under chaos: after enough fault-free gossip rounds, every
// node must hold exactly the global maximal set of all accepted writes,
// regardless of which copies were dropped, duplicated or reordered.
func TestChaosConvergence(t *testing.T) {
	for _, seed := range []int64{1, 2, 3, 42, 4242, 9999} {
		t.Run("", func(t *testing.T) {
			sc := loadExample(t, "06-chaos-seeded.json")
			sc.Name = "chaos"
			sc.Seed = seed
			res, err := Run(sc)
			if err != nil {
				t.Fatal(err)
			}

			// Oracle: take every accepted write version and reduce to the
			// global maximal set.
			oracle := map[string]register.Version{}
			for _, e := range res.Trace {
				if e.Kind == "write" {
					oracle[e.NewVersion.ID] = *e.NewVersion
				}
			}
			want := maximal(t, oracle)

			// Continue from the final state: rebuild a fresh scenario whose
			// stores we cannot mutate here, so instead verify via a second
			// healing run using gossip snapshots through the public API by
			// appending repair events to a copy scenario is impossible
			// (stores are internal); instead assert convergence by repeated
			// full broadcasts in an extended scenario.
			healed := healScenario(t, sc, res)
			final := healed.FinalState
			for _, ns := range final {
				got := ns.Keys["k"]
				if !sameIDSet(idsOf(got), want) {
					t.Fatalf("seed %d node %s=%v want oracle %v", seed, ns.Node, idsOf(got), want)
				}
			}
		})
	}
}

// healScenario reruns the original events then appends fault-free gossip
// rounds. Each round must fully settle (all delay-1 deliveries processed)
// before the next round's snapshots are taken, so updates cascade
// hop-by-hop across the cluster — an anti-entropy repair.
func healScenario(t *testing.T, base *Scenario, first *Result) *Result {
	t.Helper()
	net := *base.Network // keep the original fault injection for original events
	sc := &Scenario{
		Name:    base.Name,
		Nodes:   append([]string{}, base.Nodes...),
		Seed:    base.Seed,
		Network: &net,
		Events:  append([]EventSpec{}, base.Events...),
	}
	at := int64(40)
	for round := 0; round < len(sc.Nodes)+2; round++ {
		for _, n := range sc.Nodes {
			sc.Events = append(sc.Events, EventSpec{
				At: at, Type: "broadcast", NodeName: n,
				DropProb: ptr(0.0), DuplicateProb: ptr(0.0), Delay: ptr(int64(1)),
			})
		}
		at += 3 // allow all delay-1 deliveries to settle before round+1
	}
	res, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func maximal(t *testing.T, vs map[string]register.Version) []string {
	t.Helper()
	var all []register.Version
	for _, v := range vs {
		all = append(all, v)
	}
	var out []string
	for i, v := range all {
		dom := false
		for j, w := range all {
			if i == j {
				continue
			}
			c := vclock.Compare(v.Clock, w.Clock)
			if c == vclock.Before || (c == vclock.EqualRel && v.ID > w.ID) {
				dom = true
			}
		}
		if !dom {
			out = append(out, v.ID)
		}
	}
	return sorted(out)
}

func sameIDSet(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	m := map[string]bool{}
	for _, g := range got {
		m[g] = true
	}
	for _, w := range want {
		if !m[w] {
			return false
		}
	}
	return true
}

// Same-tick ordering: a message delivered at tick t is merged before a
// client write scheduled at the same tick t. This makes outcomes
// independent of event declaration order.
func TestSameTickDeliveryBeforeWrite(t *testing.T) {
	sc := &Scenario{
		Name:    "same-tick",
		Nodes:   []string{"n1", "n2"},
		Seed:    1,
		Network: &NetworkConfig{MinDelay: 1, MaxDelay: 1},
		Events: []EventSpec{
			{At: 1, Type: "write", NodeName: "n1", Key: "k", Value: json.RawMessage(`"a"`), Context: []string{}},
			{At: 2, Type: "send", NodeName: "n1", To: "n2", Delay: ptr(int64(3))},
			// Declared BEFORE any t=5 delivery could exist, yet it must see
			// the arriving sibling and therefore reject the blind write.
			{At: 5, Type: "write", NodeName: "n2", Key: "k", Value: json.RawMessage(`"b"`), Context: []string{}},
		},
	}
	res, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	var rejected bool
	for _, e := range res.Trace {
		if e.Kind == "write_rejected" && e.At == 5 && e.Reason == "stale_context" {
			rejected = true
		}
	}
	if !rejected {
		t.Fatal("same-tick blind write must be rejected after delivery settles")
	}
}

// Prefix determinism: appending later events must never change how
// earlier same-tick events resolve. This guards the event-priority rule
// that fixes prefixes of a simulation independent of its tail.
func TestPrefixDeterminism(t *testing.T) {
	base := loadExample(t, "06-chaos-seeded.json")
	base.Seed = 4242
	short, err := Run(base)
	if err != nil {
		t.Fatal(err)
	}
	extended := *base
	extended.Events = append(append([]EventSpec{}, base.Events...),
		EventSpec{At: 100, Type: "read", NodeName: "n1", Key: "k"})
	long, err := Run(&extended)
	if err != nil {
		t.Fatal(err)
	}
	// The first len(short.Trace) entries must be identical.
	for i := 0; i < len(short.Trace); i++ {
		a, _ := json.Marshal(short.Trace[i])
		b, _ := json.Marshal(long.Trace[i])
		if string(a) != string(b) {
			t.Fatalf("trace prefix diverged at %d:\n%s\nvs\n%s", i, a, b)
		}
	}
}

// Validation failures are hard errors, not silent rejections.
func TestValidationErrors(t *testing.T) {
	bad := []string{
		`{"name":"x","nodes":[],"events":[]}`,
		`{"name":"x","nodes":["a","a"],"events":[]}`,
		`{"name":"x","nodes":["a"],"events":[{"at":1,"type":"write","node":"a"}]}`,
		`{"name":"x","nodes":["a"],"events":[{"at":1,"type":"frob","node":"a"}]}`,
		`{"name":"x","nodes":["a"],"network":{"drop_prob":1.5},"events":[]}`,
		`{"name":"x","nodes":["a"],"events":[{"at":1,"type":"send","node":"a","to":"b"}]}`,
	}
	for _, b := range bad {
		var sc Scenario
		if err := json.Unmarshal([]byte(b), &sc); err != nil {
			t.Fatal(err)
		}
		if _, err := Run(&sc); err == nil {
			t.Fatalf("expected validation error for %s", b)
		}
	}
}

func TestRunJSONRoundTrip(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "examples", "01-offline-writes.json"))
	if err != nil {
		t.Fatal(err)
	}
	out, err := RunJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	if !json.Valid(out) || !strings.Contains(string(out), "write_rejected") {
		t.Fatal("output JSON missing expected trace")
	}
}

// ---- helpers ----

func runJSONExample(name string) ([]byte, error) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "examples", name))
	if err != nil {
		return nil, err
	}
	return RunJSON(raw)
}

func int64Seed(t *testing.T, raw []byte, seed int64) *Scenario {
	t.Helper()
	var sc Scenario
	if err := json.Unmarshal(raw, &sc); err != nil {
		t.Fatal(err)
	}
	sc.Seed = seed
	return &sc
}

func mustJSON(sc *Scenario) []byte {
	b, _ := json.Marshal(sc)
	return b
}

func ptr[T any](v T) *T { return &v }

func idsOf(vs []register.Version) []string {
	out := make([]string, len(vs))
	for i, v := range vs {
		out[i] = v.ID
	}
	return sorted(out)
}

func sorted(s []string) []string {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
	return s
}
