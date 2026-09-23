package quorum

import (
	"reflect"
	"testing"
)

func cfg(nodes []Node, rq, wq int, tDom int) Config {
	return Config{Nodes: nodes, ReadQuorum: rq, WriteQuorum: wq, TolerateDomains: tDom}
}

func TestValidate(t *testing.T) {
	tests := []struct {
		name     string
		cfg      Config
		wantErr  []string
		wantWarn []string
	}{
		{
			name: "empty nodes",
			cfg:  cfg(nil, 1, 1, 0),
			wantErr: []string{
				"nodes: at least one node is required",
				"read_quorum 1 exceeds total node weight 0: no read quorum can ever form",
				"write_quorum 1 exceeds total node weight 0: no write quorum can ever form",
			},
		},
		{
			name: "duplicate node ids",
			cfg: cfg([]Node{
				{ID: "x", Weight: 1, Domain: "a"},
				{ID: "x", Weight: 1, Domain: "b"},
			}, 1, 1, 0),
			wantErr: []string{`nodes[1]: duplicate node id "x"`},
		},
		{
			name: "negative weight",
			cfg:  cfg([]Node{{ID: "x", Weight: -2, Domain: "a"}}, 1, 1, 0),
			wantErr: []string{
				"nodes[0] (x): weight must be non-negative, got -2",
				"read_quorum 1 exceeds total node weight -2: no read quorum can ever form",
				"write_quorum 1 exceeds total node weight -2: no write quorum can ever form",
			},
		},
		{
			name: "non-positive quorums",
			cfg:  cfg([]Node{{ID: "x", Weight: 1, Domain: "a"}}, 0, -1, 0),
			wantErr: []string{
				"read_quorum: must be a positive integer, got 0",
				"write_quorum: must be a positive integer, got -1",
			},
		},
		{
			name:    "quorum exceeds total",
			cfg:     cfg([]Node{{ID: "x", Weight: 1, Domain: "a"}}, 2, 1, 0),
			wantErr: []string{"read_quorum 2 exceeds total node weight 1: no read quorum can ever form"},
		},
		{
			name: "all zero weight",
			cfg: cfg([]Node{
				{ID: "a", Weight: 0, Domain: "x"},
				{ID: "b", Weight: 0, Domain: "x"},
			}, 1, 1, 0),
			wantErr: []string{"read_quorum 1 exceeds total node weight 0: no read quorum can ever form",
				"write_quorum 1 exceeds total node weight 0: no write quorum can ever form"},
		},
		{
			name: "too many nodes",
			cfg: func() Config {
				ns := make([]Node, MaxNodes+1)
				for i := range ns {
					ns[i] = Node{ID: string(rune('a' + i)), Weight: 1, Domain: "d"}
				}
				return cfg(ns, 1, 1, 0)
			}(),
			wantErr: []string{"nodes: exhaustive analysis supports at most 20 nodes, got 21"},
		},
		{
			name:     "zero weight warning",
			cfg:      cfg([]Node{{ID: "a", Weight: 1}, {ID: "z", Weight: 0}}, 1, 1, 0),
			wantWarn: []string{"zero_weight_nodes", "missing_domain"},
		},
		{
			name:     "valid unweighted majority",
			cfg:      cfg([]Node{{ID: "a", Weight: 1, Domain: "x"}, {ID: "b", Weight: 1, Domain: "y"}, {ID: "c", Weight: 1, Domain: "z"}}, 2, 2, 1),
			wantWarn: nil,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v, errs := Validate(tt.cfg)
			if tt.wantErr != nil {
				if errs == nil {
					t.Fatalf("expected errors %v, got none", tt.wantErr)
				}
				if !reflect.DeepEqual(errs, tt.wantErr) {
					t.Fatalf("errors mismatch:\n got %v\nwant %v", errs, tt.wantErr)
				}
				return
			}
			if errs != nil {
				t.Fatalf("unexpected errors: %v", errs)
			}
			var got []string
			for _, w := range v.warnings {
				got = append(got, w.Code)
			}
			if !reflect.DeepEqual(got, tt.wantWarn) {
				t.Fatalf("warnings: got %v want %v", got, tt.wantWarn)
			}
		})
	}
}

func TestAnalyzeSafeMajority(t *testing.T) {
	c := cfg([]Node{
		{ID: "n1", Weight: 1, Domain: "a"},
		{ID: "n2", Weight: 1, Domain: "b"},
		{ID: "n3", Weight: 1, Domain: "c"},
	}, 2, 2, 1)
	r := Analyze(c)
	if !r.Valid || !r.WWSafe || !r.RWSafe {
		t.Fatalf("majority config must be safe: %+v", r.Errors)
	}
	if !r.Available || r.NumReadQuorums != 4 || r.NumWriteQuorums != 4 {
		t.Fatalf("unexpected report: avail=%v nr=%d nw=%d", r.Available, r.NumReadQuorums, r.NumWriteQuorums)
	}
	if r.MinimalCounterexample != nil {
		t.Fatal("safe config must have no counterexample")
	}
}

func TestAnalyzeWeightedRWUnsafe(t *testing.T) {
	// Weights 3,2,1, total 6. wq=4 forces n1(3)+one; rq=2 allows {n2,n3}
	// (weight 3), disjoint from a write {n1,n2} -> RW unsafe, WW safe (4+4>6).
	c := cfg([]Node{
		{ID: "n1", Weight: 3, Domain: "a"},
		{ID: "n2", Weight: 2, Domain: "b"},
		{ID: "n3", Weight: 1, Domain: "c"},
	}, 2, 4, 1)
	r := Analyze(c)
	if !r.WWSafe {
		t.Fatal("wq+wq = 8 > 6 must be WW safe")
	}
	if r.RWSafe || r.MinimalRWCounterexample == nil {
		t.Fatal("expected RW counterexample")
	}
	ce := r.MinimalRWCounterexample
	if ce.Kind != "rw" || intersects(ce.QuorumA, ce.QuorumB) {
		t.Fatalf("counterexample quorums must be disjoint: %v %v", ce.QuorumA, ce.QuorumB)
	}
	if weightOfSet(ce.QuorumA, c) < r.ReadQuorum || weightOfSet(ce.QuorumB, c) < r.WriteQuorum {
		t.Fatal("counterexample sets fail their thresholds")
	}
}

func TestMinimalCounterexampleShape(t *testing.T) {
	// r=w=1 on 3 unit nodes: minimum disjoint pair is two singletons.
	c := cfg([]Node{
		{ID: "a", Weight: 1, Domain: "a"},
		{ID: "b", Weight: 1, Domain: "b"},
		{ID: "c", Weight: 1, Domain: "c"},
	}, 1, 1, 0)
	r := Analyze(c)
	ce := r.MinimalWWCounterexample
	if ce == nil {
		t.Fatal("expected WW counterexample")
	}
	if len(ce.QuorumA) != 1 || len(ce.QuorumB) != 1 {
		t.Fatalf("minimal pair should be two singletons, got %v %v", ce.QuorumA, ce.QuorumB)
	}
}

func TestZeroWeightNodeIrrelevant(t *testing.T) {
	// n1,n2 unit + z0 zero; r=w=1: z0 must never appear in a quorum.
	c := cfg([]Node{
		{ID: "n1", Weight: 1, Domain: "a"},
		{ID: "n2", Weight: 1, Domain: "b"},
		{ID: "z0", Weight: 0, Domain: "a"},
	}, 1, 1, 0)
	r := Analyze(c)
	for _, q := range r.ReadQuorums {
		for _, id := range q {
			if id == "z0" {
				t.Fatal("zero-weight node must not appear in a reaching quorum listing")
			}
		}
	}
	ce := r.MinimalWWCounterexample
	for _, id := range append(append([]string{}, ce.QuorumA...), ce.QuorumB...) {
		if id == "z0" {
			t.Fatal("zero-weight node in minimal counterexample")
		}
	}
}

func TestWholeDomainFailure(t *testing.T) {
	// Two of three unit nodes share domain "a"; losing "a" leaves weight 1 < 2.
	c := cfg([]Node{
		{ID: "n1", Weight: 1, Domain: "a"},
		{ID: "n2", Weight: 1, Domain: "a"},
		{ID: "n3", Weight: 1, Domain: "b"},
	}, 2, 2, 1)
	r := Analyze(c)
	if r.Available {
		t.Fatal("configuration must be unavailable under one-domain outage")
	}
	if len(r.MinimalAvailabilityFailure) != 1 || r.MinimalAvailabilityFailure[0] != "a" {
		t.Fatalf("minimal availability failure = %v, want [a]", r.MinimalAvailabilityFailure)
	}
	// Losing domain b leaves {n1,n2} weight 2: still available.
	var bScenario *Scenario
	for i := range r.Scenarios {
		s := &r.Scenarios[i]
		if len(s.FailedDomains) == 1 && s.FailedDomains[0] == "b" && !s.BeyondTolerance {
			bScenario = s
		}
	}
	if bScenario == nil || !bScenario.ReadPossible || !bScenario.WritePossible {
		t.Fatal("losing domain b must preserve both quorums")
	}

	// All-domains-down reference scenario exists and allows nothing.
	var allDown *Scenario
	for i := range r.Scenarios {
		if r.Scenarios[i].BeyondTolerance {
			allDown = &r.Scenarios[i]
		}
	}
	if allDown == nil || allDown.ReadPossible || allDown.WritePossible {
		t.Fatal("expected all-domains-down scenario with no quorum possible")
	}
}

func TestToleranceZeroSkipsScenarios(t *testing.T) {
	c := cfg([]Node{
		{ID: "n1", Weight: 1, Domain: "a"},
		{ID: "n2", Weight: 1, Domain: "b"},
	}, 1, 1, 0)
	r := Analyze(c)
	if r.NumScenarios != 1 || len(r.Scenarios) != 2 {
		// exactly no-failure + all-down reference
		t.Fatalf("NumScenarios=%d listed=%d, want 1 and 2", r.NumScenarios, len(r.Scenarios))
	}
}

func weightOfSet(ids []string, c Config) int {
	m := map[string]int{}
	for _, n := range c.Nodes {
		m[n.ID] = n.Weight
	}
	sum := 0
	for _, id := range ids {
		sum += m[id]
	}
	return sum
}

func intersects(a, b []string) bool {
	m := map[string]bool{}
	for _, x := range a {
		m[x] = true
	}
	for _, x := range b {
		if m[x] {
			return true
		}
	}
	return false
}
