package main

import (
	"encoding/json"
	"fmt"
	"os"

	"fencinglease/internal/runner"
	"fencinglease/internal/sim"
)

// demos maps a built-in name to a scenario builder. All times are ticks.
// Common layout: ttl=10, heartbeat every 5, zero-delay reliable network unless
// a rule says otherwise, so traces are easy to read.
var demos = map[string]func(seed int64) *runner.Scenario{
	"renew-lost":    demoRenewLost,
	"pause-expired": demoPauseExpired,
	"late-message":  demoLateMessage,
	"duplicate":     demoDuplicate,
	"no-fence":      demoNoFence,
}

func baseNet() sim.NetConfig {
	return sim.NetConfig{MinDelay: 1, MaxDelay: 0}
}

func ip(i int) *int { return &i }

// demoRenewLost: A holds the lock with ttl=15 and renews every ~7 ticks
// (ttl/2). Its first renewal is lost in the network long enough to arrive
// after the lease has expired: the server rejects it (lease_expired), the
// local deadline also fires, B acquires fence 2, and the resource turns away
// A's stale write. A healthy path (the duplicate demo) shows that a renewal
// arriving before expiry extends the lease instead.
func demoRenewLost(seed int64) *runner.Scenario {
	return &runner.Scenario{
		Name:      "renewal-lost",
		Seed:      seed,
		MaxTime:   70,
		Network:   baseNet(),
		Resources: []runner.ResourceCfg{{Name: "acct", NodeID: "res-1", Fenced: true}},
		Nodes:     []string{"A", "B"},
		Actions: []runner.Action{
			{Time: 0, Type: "acquire", Node: "A", Resource: "acct", TTL: 15},
			{Time: 6, Type: "submit", Node: "A", Resource: "acct", Value: "a-while-held"},
			{Time: 2, Type: "add_rule", Rule: &sim.Rule{
				Name: "lose-first-renew", Action: "delay", Delay: 12,
				Match: sim.Match{From: "A", To: "lockserver", Method: "lock.renew", Nth: 1},
			}},
			{Time: 16, Type: "acquire", Node: "B", Resource: "acct", TTL: 15},
			{Time: 20, Type: "submit", Node: "B", Resource: "acct", Value: "b-wins"},
			{Time: 21, Type: "submit", Node: "A", Resource: "acct", Value: "a-zombie"},
		},
		Expect: []runner.Expect{
			{Kind: "records_exist", Node: "network", Type: "net.delay"},
			{Kind: "records_exist", Node: "lockserver", Type: "lock.renew_rejected"},
			{Kind: "accepted", Resource: "acct", Node: "A", Min: ip(1)}, // the t=6 write
			{Kind: "accepted", Resource: "acct", Node: "B", Min: ip(1)},
			{Kind: "rejected", Resource: "acct", Node: "A", Field: "fence_too_old", Min: ip(1)},
			{Kind: "last_water", Resource: "acct", Equals: 2},
		},
	}
}

// demoPauseExpired: A is paused longer than its lease. On resume it is a
// zombie holding fence 1 while B legitimately holds fence 2.
func demoPauseExpired(seed int64) *runner.Scenario {
	return &runner.Scenario{
		Name:      "pause-longer-than-lease",
		Seed:      seed,
		MaxTime:   60,
		Network:   baseNet(),
		Resources: []runner.ResourceCfg{{Name: "acct", NodeID: "res-1", Fenced: true}},
		Nodes:     []string{"A", "B"},
		Actions: []runner.Action{
			{Time: 0, Type: "acquire", Node: "A", Resource: "acct", TTL: 10},
			{Time: 4, Type: "pause", Node: "A", Until: 22},
			{Time: 12, Type: "acquire", Node: "B", Resource: "acct", TTL: 10},
			{Time: 16, Type: "submit", Node: "B", Resource: "acct", Value: "b-after-pause"},
			{Time: 24, Type: "submit", Node: "A", Resource: "acct", Value: "a-zombie"},
		},
		Expect: []runner.Expect{
			{Kind: "records_exist", Node: "A", Type: "client.lock_lost"},
			{Kind: "accepted", Resource: "acct", Node: "B", Min: ip(1)},
			{Kind: "rejected", Resource: "acct", Node: "A", Field: "fence_too_old", Min: ip(1)},
			{Kind: "last_water", Resource: "acct", Equals: 2},
			{Kind: "accepted_order", Resource: "acct", Order: []string{"B"}},
		},
	}
}

// demoLateMessage: A's renewal message is held in the network and only
// arrives after the lease has expired and B has acquired fence 2. The late
// renew is refused and A's delayed write is rejected.
func demoLateMessage(seed int64) *runner.Scenario {
	return &runner.Scenario{
		Name:      "late-message",
		Seed:      seed,
		MaxTime:   60,
		Network:   baseNet(),
		Resources: []runner.ResourceCfg{{Name: "acct", NodeID: "res-1", Fenced: true}},
		Nodes:     []string{"A", "B"},
		Actions: []runner.Action{
			{Time: 0, Type: "acquire", Node: "A", Resource: "acct", TTL: 10},
			{Time: 2, Type: "add_rule", Rule: &sim.Rule{
				Name: "hold-renew", Action: "delay", Delay: 8,
				Match: sim.Match{From: "A", To: "lockserver", Method: "lock.renew", Nth: 1},
			}},
			{Time: 11, Type: "acquire", Node: "B", Resource: "acct", TTL: 10},
			{Time: 15, Type: "submit", Node: "B", Resource: "acct", Value: "b-new-holder"},
			{Time: 17, Type: "submit", Node: "A", Resource: "acct", Value: "a-late"},
		},
		Expect: []runner.Expect{
			{Kind: "records_exist", Node: "lockserver", Type: "lock.renew_rejected"},
			{Kind: "accepted", Resource: "acct", Node: "B", Min: ip(1)},
			{Kind: "rejected", Resource: "acct", Node: "A", Field: "fence_too_old", Min: ip(1)},
			{Kind: "accepted", Resource: "acct", Node: "A", Max: ip(0)},
		},
	}
}

// demoDuplicate: a renewal RPC is duplicated in the network. Server-side
// request deduplication means the lease is extended exactly once and no
// state is corrupted; A's writes continue to be accepted.
func demoDuplicate(seed int64) *runner.Scenario {
	return &runner.Scenario{
		Name:      "duplicate-renew",
		Seed:      seed,
		MaxTime:   30,
		Network:   baseNet(),
		Resources: []runner.ResourceCfg{{Name: "acct", NodeID: "res-1", Fenced: true}},
		Nodes:     []string{"A"},
		Actions: []runner.Action{
			{Time: 0, Type: "acquire", Node: "A", Resource: "acct", TTL: 10},
			{Time: 12, Type: "submit", Node: "A", Resource: "acct", Value: "a-ok"},
			{Time: 2, Type: "add_rule", Rule: &sim.Rule{
				Name: "dup-renew", Action: "duplicate", DupDelay: 2,
				Match: sim.Match{From: "A", To: "lockserver", Method: "lock.renew", Nth: 1},
			}},
		},
		Expect: []runner.Expect{
			{Kind: "records_exist", Node: "lockserver", Type: "lock.served_from_cache"},
			{Kind: "accepted", Resource: "acct", Node: "A", Min: ip(1)},
			{Kind: "rejected", Resource: "acct", Min: ip(0), Max: ip(0)},
		},
	}
}

// demoNoFence is the control experiment: the resource ignores fence tokens.
// A is paused past its lease, B takes over and writes, then zombie A writes
// with the old token — and it is accepted, corrupting order. The built-in
// monotonicity invariant FAILS here by design.
func demoNoFence(seed int64) *runner.Scenario {
	return &runner.Scenario{
		Name:      "no-fencing-baseline-fails",
		Seed:      seed,
		MaxTime:   60,
		Network:   baseNet(),
		Resources: []runner.ResourceCfg{{Name: "acct", NodeID: "res-1", Fenced: false}},
		Nodes:     []string{"A", "B"},
		Actions: []runner.Action{
			{Time: 0, Type: "acquire", Node: "A", Resource: "acct", TTL: 10},
			{Time: 4, Type: "pause", Node: "A", Until: 22},
			{Time: 12, Type: "acquire", Node: "B", Resource: "acct", TTL: 10},
			{Time: 16, Type: "submit", Node: "B", Resource: "acct", Value: "b-legit"},
			{Time: 24, Type: "submit", Node: "A", Resource: "acct", Value: "a-stale-corrupts"},
		},
		Expect: []runner.Expect{
			{Kind: "accepted", Resource: "acct", Node: "A", Min: ip(1)},
			{Kind: "accepted_order", Resource: "acct", Order: []string{"B", "A"}},
		},
	}
}

// fuzzSummary is the aggregated output of "fls demo fuzz".
type fuzzSummary struct {
	Name  string    `json:"name"`
	OK    bool      `json:"ok"`
	Seeds int       `json:"seeds"`
	Runs  []fuzzRun `json:"runs"`
}

type fuzzRun struct {
	Seed       int64    `json:"seed"`
	OK         bool     `json:"ok"`
	Commits    int      `json:"commits"`
	Rejected   int      `json:"rejected"`
	Grants     int      `json:"grants"`
	Violations []string `json:"violations,omitempty"`
}

// runFuzz generates lossy/duplicated/reordered workloads for several seeds.
func runFuzz(seeds int) int {
	summary := fuzzSummary{Name: "fuzz", OK: true, Seeds: seeds}
	for s := int64(1); s <= int64(seeds); s++ {
		sc := buildFuzzScenario(s)
		res := runner.Run(sc)
		fr := fuzzRun{Seed: s, OK: res.OK, Commits: len(res.Commits)}
		for _, c := range res.Checks {
			if !c.Pass {
				fr.Violations = append(fr.Violations, c.Kind+": "+c.Detail)
			}
		}
		for _, t := range res.Traces {
			switch t.Type {
			case "resource.rejected":
				fr.Rejected++
			case "lock.granted":
				fr.Grants++
			}
		}
		if !fr.OK {
			summary.OK = false
		}
		summary.Runs = append(summary.Runs, fr)
	}
	raw, _ := json.MarshalIndent(summary, "", "  ")
	fmt.Println(string(raw))
	if !summary.OK {
		fmt.Fprintln(os.Stderr, "FUZZ FAIL")
		return 1
	}
	fmt.Fprintf(os.Stderr, "FUZZ PASS: %d seeds\n", seeds)
	return 0
}

// cmdDemoJSON prints the scenario JSON (without running it) so examples/ can
// be generated from the exact built-in definitions.
func cmdDemoJSON(args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "usage: fls demo-json <name>")
		return 2
	}
	builder, ok := demos[args[0]]
	if !ok {
		fmt.Fprintf(os.Stderr, "unknown demo %q\n", args[0])
		return 2
	}
	raw, err := json.MarshalIndent(builder(1), "", "  ")
	if err != nil {
		panic(err)
	}
	fmt.Println(string(raw))
	return 0
}

// buildFuzzScenario produces a fixed-shape lossy workload. All randomness
// used at runtime flows from the seed via the simulator RNG; the shape itself
// is fixed, so failures are reproducible from the seed. Initial acquires are
// issued a few times so a dropped grant reply (which the one-shot client here
// does not retry) cannot leave a node tokenless for the whole run; repeated
// acquire attempts by the same holder are either re-entrant or idempotent.
func buildFuzzScenario(s int64) *runner.Scenario {
	net := sim.NetConfig{
		MinDelay: 1, MaxDelay: 2,
		LossProb: 0.15, DuplicateProb: 0.08, ReorderProb: 0.08,
	}
	acq := func(t int64, who string) runner.Action {
		return runner.Action{Time: t, Type: "acquire", Node: who, Resource: "acct", TTL: 18}
	}
	return &runner.Scenario{
		Name:      fmt.Sprintf("fuzz-%d", s),
		Seed:      s,
		MaxTime:   200,
		Network:   net,
		Resources: []runner.ResourceCfg{{Name: "acct", NodeID: "res-1", Fenced: true}},
		Nodes:     []string{"A", "B", "C"},
		Actions: []runner.Action{
			acq(0, "A"), acq(3, "A"),
			acq(20, "B"), acq(23, "B"),
			acq(40, "C"), acq(43, "C"),
			{Time: 2, Type: "compete", Node: "A", Resource: "acct", Steps: 45, Every: 4, Prefix: "a"},
			{Time: 22, Type: "compete", Node: "B", Resource: "acct", Steps: 40, Every: 4, Prefix: "b"},
			{Time: 42, Type: "compete", Node: "C", Resource: "acct", Steps: 35, Every: 4, Prefix: "c"},
			// A pauses past a lease; zombie writes afterwards must all fail.
			{Time: 60, Type: "pause", Node: "A", Until: 90},
		},
	}
}
