package sim

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// helper: run a scenario from inline JSON.
func runJSON(t *testing.T, data string) *Result {
	t.Helper()
	res, err := RunJSON([]byte(data))
	if err != nil {
		t.Fatalf("RunJSON: %v", err)
	}
	return res
}

func assertAllInvariants(t *testing.T, res *Result) {
	t.Helper()
	for _, c := range res.Report.Invariants {
		if !c.Pass {
			t.Errorf("invariant %q failed: %s", c.Name, c.Detail)
		}
	}
}

// TestScenarioRenewalLost: dropped renewals force a lease takeover, and the
// deposed holder's later write with its old fence is rejected by the
// resource even though the lock takeover happened without the holder's
// involvement.
func TestScenarioRenewalLost(t *testing.T) {
	data := `{
	  "name":"t-renewal-lost","seed":1,"ttl_ms":2000,"heartbeat_ms":700,
	  "retry_gap_ms":200,"end_ms":5000,
	  "network":{"min_latency_ms":10,"max_latency_ms":10,"rules":[
	    {"type":"drop","src":"A","dst":"L","req_type":"renew","from":710,"until":1430}
	  ]},
	  "clients":[{"id":"A"},{"id":"B"}],
	  "actions":[
	    {"at_ms":0,"client":"A","op":"acquire"},
	    {"at_ms":300,"client":"A","op":"submit","value":"a1","resource":"R"},
	    {"at_ms":1600,"client":"B","op":"acquire","retry":true},
	    {"at_ms":2600,"client":"B","op":"submit","value":"b2","resource":"R"},
	    {"at_ms":3500,"client":"A","op":"submit","value":"zombie","resource":"R","force_submit":true}
	  ]
	}`
	res := runJSON(t, data)
	assertAllInvariants(t, res)
	rep := res.Report

	if rep.Lock.Grants != 2 {
		t.Fatalf("want 2 grants, got %d", rep.Lock.Grants)
	}
	if rep.Lock.Expirations != 1 {
		t.Fatalf("want 1 expiration, got %d", rep.Lock.Expirations)
	}
	commits := rep.Resource.Committed
	if len(commits) != 2 || commits[0].Fence != 1 || commits[1].Fence != 2 {
		t.Fatalf("want commits fences [1 2], got %+v", commits)
	}
	if len(rep.Resource.Rejected) != 1 {
		t.Fatalf("want exactly 1 stale rejection, got %+v", rep.Resource.Rejected)
	}
	if rep.Resource.Rejected[0].Fence != 1 || rep.Resource.Rejected[0].Reason != ResultStaleFence {
		t.Fatalf("want stale fence 1 rejection, got %+v", rep.Resource.Rejected[0])
	}
}

// TestScenarioPauseLongerThanTTL: a frozen client cannot renew; another
// client acquires while it is frozen; the zombie write after resume is
// rejected by the fence on the resource.
func TestScenarioPauseLongerThanTTL(t *testing.T) {
	data := `{
	  "name":"t-pause","seed":2,"ttl_ms":1000,"heartbeat_ms":400,
	  "retry_gap_ms":200,"end_ms":5000,
	  "network":{"min_latency_ms":10,"max_latency_ms":10,"rules":[]},
	  "clients":[{"id":"A"},{"id":"B"}],
	  "actions":[
	    {"at_ms":0,"client":"A","op":"acquire"},
	    {"at_ms":200,"client":"A","op":"submit","value":"a1","resource":"R"},
	    {"at_ms":500,"client":"A","op":"pause","duration_ms":2500},
	    {"at_ms":900,"client":"B","op":"acquire","retry":true},
	    {"at_ms":2200,"client":"B","op":"submit","value":"b2","resource":"R"},
	    {"at_ms":2700,"client":"B","op":"release"},
	    {"at_ms":3050,"client":"A","op":"submit","value":"zombie","resource":"R","force_submit":true},
	    {"at_ms":3400,"client":"A","op":"acquire"},
	    {"at_ms":4000,"client":"A","op":"submit","value":"a3","resource":"R"}
	  ]
	}`
	res := runJSON(t, data)
	assertAllInvariants(t, res)
	rep := res.Report

	fences := make([]int64, 0, len(rep.Resource.Committed))
	for _, c := range rep.Resource.Committed {
		fences = append(fences, c.Fence)
	}
	wantFences := []int64{1, 2, 3}
	if len(fences) != 3 {
		t.Fatalf("want fences %v, got %v", wantFences, fences)
	}
	for i := range wantFences {
		if fences[i] != wantFences[i] {
			t.Fatalf("want fences %v, got %v", wantFences, fences)
		}
	}
	if len(rep.Resource.Rejected) != 1 || rep.Resource.Rejected[0].Fence != 1 {
		t.Fatalf("want one stale-fence-1 rejection, got %+v", rep.Resource.Rejected)
	}

	// While A is paused (500..3000) it must have sent no packets at all:
	// the harness freezes heartbeats and actions.
	for _, ev := range rep.Trace {
		d := ev.Detail
		if d == nil {
			continue
		}
		if d["src"] == "A" && ev.Time >= 500 && ev.Time < 3000 {
			t.Errorf("client A emitted traffic at %d while paused: %s %v", ev.Time, ev.Event, d)
		}
	}
}

// TestLateAndDuplicateMessages: a late renewal after expiry is answered
// expired, its duplicate has no effect, and a duplicated stale submit is
// rejected twice. Committed fences are 1,2,3.
func TestLateAndDuplicateMessages(t *testing.T) {
	data := `{
	  "name":"t-late-dup","seed":3,"ttl_ms":2000,"heartbeat_ms":700,
	  "retry_gap_ms":200,"end_ms":6000,
	  "network":{"min_latency_ms":10,"max_latency_ms":10,"rules":[
	    {"type":"drop","src":"A","dst":"L","req_type":"renew","from":710,"until":1430},
	    {"type":"duplicate","src":"A","dst":"L","req_type":"renew","from":2110,"until":2130,"delay_ms":700},
	    {"type":"duplicate","src":"A","dst":"R","req_type":"submit","from":3440,"until":3470}
	  ]},
	  "clients":[{"id":"A"},{"id":"B"}],
	  "actions":[
	    {"at_ms":0,"client":"A","op":"acquire"},
	    {"at_ms":400,"client":"A","op":"submit","value":"a1","resource":"R"},
	    {"at_ms":1800,"client":"B","op":"acquire","retry":true},
	    {"at_ms":2600,"client":"B","op":"submit","value":"b2","resource":"R"},
	    {"at_ms":3100,"client":"B","op":"release"},
	    {"at_ms":3450,"client":"A","op":"submit","value":"late","resource":"R","force_submit":true},
	    {"at_ms":4200,"client":"A","op":"acquire"},
	    {"at_ms":4800,"client":"A","op":"submit","value":"a3","resource":"R"}
	  ]
	}`
	res := runJSON(t, data)
	assertAllInvariants(t, res)
	rep := res.Report
	if rep.NetworkStats.Dropped < 2 {
		t.Errorf("want >=2 dropped renewals, got %d", rep.NetworkStats.Dropped)
	}
	if rep.NetworkStats.Duplicated < 2 {
		t.Errorf("want >=2 duplicates, got %d", rep.NetworkStats.Duplicated)
	}
	if len(rep.Resource.Rejected) != 2 {
		t.Fatalf("want stale submit rejected twice, got %+v", rep.Resource.Rejected)
	}
	for _, rj := range rep.Resource.Rejected {
		if rj.Fence != 1 {
			t.Errorf("rejection fence = %d, want 1", rj.Fence)
		}
	}
}

// TestDuplicateRenewDoesNotExtendTwice: the lock service de-duplicates a
// network-duplicated renewal request: the second copy returns success but
// does not move the expiry again.
func TestDuplicateRenewDoesNotExtendTwice(t *testing.T) {
	data := `{
	  "name":"t-dup-renew","seed":4,"ttl_ms":2000,"heartbeat_ms":600,
	  "retry_gap_ms":200,"end_ms":2500,
	  "network":{"min_latency_ms":10,"max_latency_ms":10,"rules":[
	    {"type":"duplicate","src":"A","dst":"L","req_type":"renew","from":600,"until":620}
	  ]},
	  "clients":[{"id":"A"}],
	  "actions":[
	    {"at_ms":0,"client":"A","op":"acquire"}
	  ]
	}`
	res := runJSON(t, data)
	assertAllInvariants(t, res)

	var dedups, renews int
	var expiry Time
	for _, ev := range res.Report.Trace {
		switch ev.Event {
		case "lock.renew.dedup":
			dedups++
		case "lock.renew":
			renews++
			expiry = Time(int64Field(ev, "expire_at"))
		}
	}
	if dedups != 1 {
		t.Fatalf("want 1 dedup, got %d", dedups)
	}
	if renews < 1 {
		t.Fatalf("want at least 1 real renew, got %d", renews)
	}
	// First renewal lands at ~610: expiry 2610. The duplicate at ~670 must
	// not extend to ~2670.
	if expiry > 2620 {
		t.Fatalf("duplicate renewal moved expiry to %d", expiry)
	}
}

// TestDeterminism: same seed and inputs must produce byte-identical reports.
func TestDeterminism(t *testing.T) {
	// Read scenario 3 from disk to exercise the actual JSON interface.
	path := filepath.Join("..", "..", "scenarios", "3-late-and-duplicate-messages.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("scenario file not available: %v", err)
	}
	var prev []byte
	for i := 0; i < 3; i++ {
		res, err := RunJSON(raw)
		if err != nil {
			t.Fatal(err)
		}
		buf, err := json.Marshal(res.Report)
		if err != nil {
			t.Fatal(err)
		}
		if prev != nil && string(buf) != string(prev) {
			t.Fatalf("run %d differs from run 0", i)
		}
		prev = buf
	}
}

// TestDeterminismDifferentSeedsCanDiffer: sanity for the probabilistic path;
// not asserting a difference (seeds may coincide), but invariants hold for
// many seeds under background fault noise.
func TestInvariantsUnderProbabilisticFaults(t *testing.T) {
	for seed := uint64(1); seed <= 40; seed++ {
		s := &Scenario{
			Name: "fuzz", Seed: seed, TTL: 1500, Heartbeat: 500, RetryGap: 150, EndAt: 9000,
			Network: Network{
				MinLatency: 2, MaxLatency: 25,
				DropProb:      0.12,
				DuplicateProb: 0.10,
				DuplicateGap:  40,
			},
			Clients: []ClientSpec{{ID: "A"}, {ID: "B"}, {ID: "C"}},
			Actions: []ActionSpec{
				{At: 0, Client: "A", Op: OpAcquire, Retry: true},
				{At: 500, Client: "A", Op: OpSubmit, Value: "a", Resource: ResourceID},
				{At: 1500, Client: "B", Op: OpAcquire, Retry: true},
				{At: 2800, Client: "B", Op: OpSubmit, Value: "b", Resource: ResourceID},
				{At: 3000, Client: "A", Op: OpSubmit, Value: "zombie", Resource: ResourceID, ForceSubmit: true},
				{At: 3300, Client: "B", Op: OpRelease},
				{At: 3500, Client: "C", Op: OpAcquire, Retry: true},
				{At: 4700, Client: "C", Op: OpSubmit, Value: "c", Resource: ResourceID},
			},
		}
		res, err := Run(s)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		assertAllInvariants(t, res)
		// Fences on the resource never decrease (equal = repeated writes by
		// the same holder).
		var prev int64
		for _, c := range res.Report.Resource.Committed {
			if c.Fence < prev {
				t.Fatalf("seed %d: commit fence %d after %d", seed, c.Fence, prev)
			}
			prev = c.Fence
		}
	}
}

// TestRejectFenceZero: a write that never carried a fence is refused.
func TestRejectFenceZero(t *testing.T) {
	s := &Scenario{
		Name: "zero", Seed: 1, TTL: 1000, Heartbeat: 400, RetryGap: 100, EndAt: 1000,
		Network: Network{MinLatency: 1, MaxLatency: 1},
		Clients: []ClientSpec{{ID: "A"}},
		Actions: []ActionSpec{
			{At: 0, Client: "A", Op: OpSubmit, Value: "no-lease", Resource: ResourceID, ForceSubmit: true},
		},
	}
	res, err := Run(s)
	if err != nil {
		t.Fatal(err)
	}
	assertAllInvariants(t, res)
	if len(res.Report.Resource.Rejected) != 1 {
		t.Fatalf("want 1 rejection, got %+v", res.Report.Resource.Rejected)
	}
	if got := res.Report.Resource.Rejected[0].Reason; got != ResultFenceZero {
		t.Fatalf("reason=%s want %s", got, ResultFenceZero)
	}
}

// TestValidation rejects malformed scenarios.
func TestValidation(t *testing.T) {
	base := Scenario{
		Name: "v", Seed: 1, TTL: 100, Heartbeat: 40, RetryGap: 10, EndAt: 1000,
		Network: Network{MinLatency: 1, MaxLatency: 2},
		Clients: []ClientSpec{{ID: "A"}},
	}
	cases := []func(s *Scenario){
		func(s *Scenario) { s.Name = "" },
		func(s *Scenario) { s.Heartbeat = s.TTL },
		func(s *Scenario) { s.TTL = -1 },
		func(s *Scenario) { s.Network.MaxLatency = 0 },
		func(s *Scenario) { s.Clients[0].ID = LockID },
		func(s *Scenario) { s.Actions = []ActionSpec{{At: 1, Client: "X", Op: OpAcquire}} },
		func(s *Scenario) { s.Actions = []ActionSpec{{At: 1, Client: "A", Op: "bogus"}} },
		func(s *Scenario) { s.Actions = []ActionSpec{{At: 1, Client: "A", Op: OpPause}} },
	}
	for i, mutate := range cases {
		s := base
		mutate(&s)
		if _, err := Run(&s); err == nil {
			t.Errorf("case %d: expected validation error", i)
		}
	}
}

// TestClockDoesNotUseWallTime: a simulation must run deterministically and far
// faster than its modeled duration; it must never read the wall clock. Here
// we simply assert a 6000ms scenario completes in well under real 6000ms.
func TestClockDoesNotUseWallTime(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "scenarios", "2-pause-longer-than-ttl.json"))
	if err != nil {
		t.Skip(err)
	}
	start := time.Now()
	res, err := RunJSON(data)
	if err != nil {
		t.Fatal(err)
	}
	if d := time.Since(start); d > 250*time.Millisecond {
		t.Fatalf("simulation took %v real time", d)
	}
	if res.Report.EndTime < 3000 {
		t.Fatalf("modeled end time too small: %d", res.Report.EndTime)
	}
}

// TestScenarioFilesOnDisk: every shipped scenario runs with all invariants.
func TestScenarioFilesOnDisk(t *testing.T) {
	dir := filepath.Join("..", "..", "scenarios")
	files, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 4 {
		t.Fatalf("expected >=4 scenario files, got %d", len(files))
	}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		res, err := RunJSON(raw)
		if err != nil {
			t.Fatalf("%s: %v", filepath.Base(f), err)
		}
		assertAllInvariants(t, res)
		if !strings.Contains(filepath.Base(f), "happy") {
			if len(res.Report.Resource.Rejected) == 0 {
				t.Errorf("%s: hostile scenario produced no resource rejections", filepath.Base(f))
			}
		}
	}
}
