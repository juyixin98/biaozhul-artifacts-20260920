package sim

import (
	"encoding/json"
	"testing"

	"chsim/internal/ring"
)

func baseScenario() Scenario {
	return Scenario{
		Seed:   7,
		Vnodes: 128,
		Nodes: []ring.Node{
			{ID: "n1", Weight: 1}, {ID: "n2", Weight: 1}, {ID: "n3", Weight: 2},
		},
		Network:     NetConfig{BaseDelayMs: 2, JitterMs: 4, DropRate: 0.02, DupRate: 0.02},
		Transfer:    TransferConfig{BatchSize: 16, Concurrency: 4, FetchTimeoutMs: 60},
		OpTimeoutMs: 150,
		Clients: []ClientConfig{
			{ID: "c1", StartMs: 0, Ops: 200, IntervalMs: 5, Keys: 50, ReadEvery: 5},
			{ID: "c2", StartMs: 2, Ops: 200, IntervalMs: 5, Keys: 50, ReadEvery: 5},
			{ID: "c3", StartMs: 4, Ops: 200, IntervalMs: 5, Keys: 50, ReadEvery: 5},
		},
	}
}

func runOK(t *testing.T, sc Scenario) *Result {
	t.Helper()
	res, err := Run(sc)
	if err != nil {
		t.Fatalf("Run error: %v", err)
	}
	if !res.Verification.Pass {
		t.Fatalf("verification failed: %+v\nstats: %+v", res.Verification, res.Stats)
	}
	return res
}

func TestScaleOut(t *testing.T) {
	sc := baseScenario()
	sc.Ops = []Op{{T: 500, Op: "add_node", NodeID: "n4", Weight: 1}}
	res := runOK(t, sc)

	if res.Stats.WritesConfirmed != res.Stats.WritesIssued {
		t.Fatalf("not all writes confirmed: %+v", res.Stats)
	}
	if len(res.Stats.Barriers) != 1 {
		t.Fatalf("want exactly 1 barrier, got %d", len(res.Stats.Barriers))
	}
	b := res.Stats.Barriers[0]
	if b.Tasks == 0 || res.Stats.UniqueKeysMigrated == 0 {
		t.Fatalf("migration did no work: %+v", b)
	}
	if res.Stats.UniqueKeysMigrated >= 50 {
		t.Fatalf("expected only a fraction of 50 keys to move, got %d", res.Stats.UniqueKeysMigrated)
	}
	if res.Stats.KeysMigrated < res.Stats.UniqueKeysMigrated {
		t.Fatal("shipped records fewer than unique keys")
	}
	if b.TimeMs <= 500 {
		t.Fatal("barrier fired before migration request")
	}
}

func TestRemoveNodeSafe(t *testing.T) {
	sc := baseScenario()
	sc.Ops = []Op{{T: 1200, Op: "remove_node", NodeID: "n1"}}
	res := runOK(t, sc)

	if res.Stats.NodesDecommissioned != 1 {
		t.Fatalf("want 1 decommissioned node, got %d", res.Stats.NodesDecommissioned)
	}
	for _, id := range res.FinalNodes {
		if id == "n1" {
			t.Fatal("removed node still in final ring")
		}
	}
	if len(res.Verification.RemovalViolations) != 0 {
		t.Fatalf("removal safety violated: %+v", res.Verification.RemovalViolations)
	}
	// Regression guard: with a healthy hash the removed node must have owned
	// a share of the keyspace, so the migration must have moved real data.
	if res.Stats.UniqueKeysMigrated == 0 {
		t.Fatal("remove_node migrated 0 keys; expected a non-empty transfer")
	}
}

func TestMigrationInterrupt(t *testing.T) {
	sc := baseScenario()
	sc.Ops = []Op{
		{T: 300, Op: "add_node", NodeID: "n4"},
		{T: 310, Op: "pause_migration"},
		{T: 900, Op: "resume_migration"},
	}
	res := runOK(t, sc)

	if len(res.Stats.Barriers) != 1 {
		t.Fatalf("want 1 barrier, got %d", len(res.Stats.Barriers))
	}
	if res.Stats.Barriers[0].TimeMs <= 900 {
		t.Fatalf("barrier at %d despite migration paused until 900", res.Stats.Barriers[0].TimeMs)
	}
	if res.Stats.ReadsCompleted == 0 {
		t.Fatal("no reads completed")
	}
	if res.Stats.StaleReads != 0 {
		t.Fatalf("dual-read returned stale data: %+v", res.Verification.StaleReads)
	}
}

func TestConcurrentWritesContendedKeys(t *testing.T) {
	sc := baseScenario()
	for i := range sc.Clients {
		sc.Clients[i].Keys = 4
		sc.Clients[i].ReadEvery = 0
		sc.Clients[i].Ops = 150
	}
	sc.Clients = append(sc.Clients, ClientConfig{ID: "c4", StartMs: 1, Ops: 150, IntervalMs: 5, Keys: 4})
	res := runOK(t, sc)

	if res.Stats.WritesConfirmed != 600 {
		t.Fatalf("want 600 confirmed writes, got %d (failed=%d)",
			res.Stats.WritesConfirmed, res.Stats.WritesFailed)
	}
	if res.Verification.KeysChecked != 4 {
		t.Fatalf("want 4 keys checked, got %d", res.Verification.KeysChecked)
	}
}

func TestChaoticNetwork(t *testing.T) {
	sc := baseScenario()
	sc.Seed = 123
	sc.Network = NetConfig{BaseDelayMs: 3, JitterMs: 15, DropRate: 0.30, DupRate: 0.15}
	sc.Ops = []Op{{T: 400, Op: "add_node", NodeID: "n4"}}
	res := runOK(t, sc)

	if res.Stats.MessagesDropped == 0 {
		t.Fatal("expected dropped messages")
	}
	if res.Stats.MessagesDuplicated == 0 {
		t.Fatal("expected duplicated messages")
	}
	if res.Stats.Retries == 0 {
		t.Fatal("expected retries")
	}
	if res.Stats.WritesFailed != 0 || res.Stats.ReadsFailed != 0 {
		t.Fatalf("requests failed despite retries: %+v", res.Stats)
	}
}

func TestMultiPageMigration(t *testing.T) {
	// Dense keyspace + tiny batches force each moved interval to span several
	// fetch/put pages. A regression where inflight was incremented per page
	// wedged the scheduler after the first couple of pages.
	sc := Scenario{
		Seed: 11, Vnodes: 64,
		Nodes:       []ring.Node{{ID: "n1"}, {ID: "n2"}, {ID: "n3"}},
		Network:     NetConfig{BaseDelayMs: 1, JitterMs: 3, DropRate: 0.03, DupRate: 0.02},
		Transfer:    TransferConfig{BatchSize: 3, Concurrency: 2, FetchTimeoutMs: 40},
		OpTimeoutMs: 150,
		Clients: []ClientConfig{
			{ID: "c1", StartMs: 0, Ops: 2000, IntervalMs: 1, Keys: 2000},
		},
		Ops: []Op{{T: 1500, Op: "add_node", NodeID: "n4"}},
	}
	res := runOK(t, sc)

	if len(res.Stats.Barriers) != 1 {
		t.Fatalf("want 1 barrier, got %d (migration wedged?)", len(res.Stats.Barriers))
	}
	// A 4th equal node takes roughly 1/4 of the 2000 keys.
	if got := res.Stats.UniqueKeysMigrated; got < 300 || got > 700 {
		t.Fatalf("multi-page migration moved %d keys, want ~500", got)
	}
	// Records shipped must cover every unique moved key even with retransmits.
	if res.Stats.KeysMigrated < res.Stats.UniqueKeysMigrated {
		t.Fatal("shipped records fewer than unique moved keys")
	}
}

func TestScaleOutThenRemove(t *testing.T) {
	sc := baseScenario()
	sc.Ops = []Op{
		{T: 300, Op: "add_node", NodeID: "n4"},
		{T: 320, Op: "pause_migration"},
		{T: 700, Op: "resume_migration"},
		{T: 1100, Op: "remove_node", NodeID: "n2"},
	}
	res := runOK(t, sc)

	if len(res.Stats.Barriers) != 2 {
		t.Fatalf("want 2 barriers, got %d: %+v", len(res.Stats.Barriers), res.Stats.Barriers)
	}
	if res.Stats.NodesDecommissioned != 1 {
		t.Fatalf("want 1 decommission, got %d", res.Stats.NodesDecommissioned)
	}
}

func TestDeterministic(t *testing.T) {
	mk := func() Scenario {
		sc := baseScenario()
		sc.Network.DropRate = 0.1
		sc.Network.DupRate = 0.05
		sc.Ops = []Op{
			{T: 300, Op: "add_node", NodeID: "n4"},
			{T: 330, Op: "pause_migration"},
			{T: 850, Op: "resume_migration"},
			{T: 1200, Op: "remove_node", NodeID: "n1"},
		}
		return sc
	}
	r1, err := Run(mk())
	if err != nil {
		t.Fatal(err)
	}
	r2, err := Run(mk())
	if err != nil {
		t.Fatal(err)
	}
	j1, _ := json.Marshal(r1)
	j2, _ := json.Marshal(r2)
	if string(j1) != string(j2) {
		t.Fatalf("same scenario produced different results:\n%s\nvs\n%s", j1, j2)
	}
	if !r1.Verification.Pass {
		t.Fatalf("scenario failed verification: %s", j1)
	}
}

func TestRemoveLastNodeRejected(t *testing.T) {
	sc := Scenario{
		Seed: 1, Vnodes: 32,
		Nodes:    []ring.Node{{ID: "only"}},
		Network:  NetConfig{BaseDelayMs: 1},
		Transfer: TransferConfig{BatchSize: 8, Concurrency: 1, FetchTimeoutMs: 50},
		Clients:  []ClientConfig{{ID: "c1", StartMs: 0, Ops: 10, IntervalMs: 2, Keys: 3}},
		Ops:      []Op{{T: 20, Op: "remove_node", NodeID: "only"}},
	}
	res, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Errors) == 0 {
		t.Fatal("expected scenario error when removing the last node")
	}
	if res.Verification.Pass {
		t.Fatal("verification should not pass with recorded scenario errors")
	}
}

func TestValidationErrors(t *testing.T) {
	if _, err := Run(Scenario{}); err == nil {
		t.Fatal("empty scenario should be rejected")
	}
	sc := baseScenario()
	sc.Network.DropRate = 1.0
	if _, err := Run(sc); err == nil {
		t.Fatal("drop_rate=1 should be rejected")
	}
}
