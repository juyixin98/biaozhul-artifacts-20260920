package scenario

import (
	"testing"

	"pilab/scheduler"
)

// TestAllScenariosRun exercises every built-in scenario.
func TestAllScenariosRun(t *testing.T) {
	for _, sc := range All() {
		t.Run(string(sc.ID), func(t *testing.T) {
			r, err := Run(sc)
			if err != nil {
				t.Fatalf("run error: %v", err)
			}
			if len(r.Events) == 0 {
				t.Fatal("no events produced")
			}
			switch sc.ID {
			case DeadlockABBA:
				if !r.Deadlocked {
					t.Errorf("expected deadlock for %s", sc.ID)
				}
			default:
				if !r.Completed {
					t.Errorf("expected completion for %s: dead=%v fatal=%q",
						sc.ID, r.Deadlocked, r.Fatal)
				}
			}
		})
	}
}

// TestClassicInversionComparison quantifies the inversion and its mitigation.
func TestClassicInversionComparison(t *testing.T) {
	c, err := CompareInversion()
	if err != nil {
		t.Fatal(err)
	}
	// Without inheritance H finishes later than with inheritance.
	if c.NoneMetric.FinishTick <= c.PIPMetric.FinishTick {
		t.Errorf("PIP should finish H sooner: none=%d pip=%d",
			c.NoneMetric.FinishTick, c.PIPMetric.FinishTick)
	}
	if c.FinishDelta <= 0 {
		t.Errorf("expected positive time saved, got %d", c.FinishDelta)
	}
	// Under PIP the lock owner L (not H) must have received a priority boost;
	// in none mode no boost events exist at all.
	var lBoosts int
	for _, tr := range c.PIP.Tasks {
		if tr.ID == "L" {
			lBoosts = tr.PriorityBoosts
		}
	}
	if lBoosts == 0 {
		t.Error("expected lock owner L to be boosted under PIP")
	}
	for _, e := range c.NonePIP.Events {
		if e.Type == scheduler.EvPriorityBoost {
			t.Fatal("none mode must not produce a boost")
		}
	}
}

// TestThreeLevelChainEvents asserts the transitive 1->5->9 propagation exists.
func TestThreeLevelChainEvents(t *testing.T) {
	sc, ok := Get(ChainInherit)
	if !ok {
		t.Fatal("missing chain scenario")
	}
	r, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	changes := map[string][]int{}
	for _, e := range r.Events {
		if e.Type == scheduler.EvPriorityBoost || e.Type == scheduler.EvPriorityReset {
			changes[e.Task] = append(changes[e.Task], e.New)
		}
	}
	got9 := false
	for _, v := range changes["T2"] {
		if v == 9 {
			got9 = true
		}
	}
	if !got9 {
		t.Errorf("lowest task T2 never reached priority 9 transitively: %v", changes)
	}
}
