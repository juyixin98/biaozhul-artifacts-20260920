package simulator

// Scenario is a named, ready-to-run workload with a human explanation.
type Scenario struct {
	ID          string     `json:"id"`
	Name        string     `json:"name"`
	Description string     `json:"description"`
	Narrative   string     `json:"narrative"`
	Tasks       []TaskSpec `json:"tasks"`
}

// Scenarios returns the built-in demonstration workloads.
func Scenarios() []Scenario {
	return []Scenario{
		{
			ID:   "classic",
			Name: "Classic priority inversion with medium interference",
			Description: "High H waits for a mutex held by low L; a bursty " +
				"medium M delays L. Without inheritance H is indirectly blocked " +
				"by M; with inheritance L runs at H's priority and finishes fast.",
			Narrative: "L(prio1) grabs A at t0 for 4 ticks. H(prio3), released " +
				"at t1, needs A. M(prio2), released at t2, only wants CPU. " +
				"Without PIP, M preempts L and H blocks for 8 ticks; with PIP, L " +
				"inherits priority 3 and H blocks for only 3 ticks.",
			Tasks: []TaskSpec{
				{Name: "L", Priority: 1, ReleaseTime: 0, Steps: []Step{
					{Op: OpLock, Resource: "A"},
					{Op: OpCompute, Duration: 4},
					{Op: OpUnlock, Resource: "A"},
					{Op: OpCompute, Duration: 1},
				}},
				{Name: "M", Priority: 2, ReleaseTime: 2, Steps: []Step{
					{Op: OpCompute, Duration: 5},
				}},
				{Name: "H", Priority: 3, ReleaseTime: 1, Steps: []Step{
					{Op: OpLock, Resource: "A"},
					{Op: OpCompute, Duration: 3},
					{Op: OpUnlock, Resource: "A"},
				}},
			},
		},
		{
			ID:   "nested-chain",
			Name: "Transitive inheritance across a nested lock chain",
			Description: "H waits on A held by X; X itself blocks on B held by " +
				"L. Inheritance must propagate transitively H -> X -> L so that " +
				"L beats the interfering medium task M.",
			Narrative: "L(prio1) holds B; X(prio2) holds A then blocks on B; " +
				"H(prio3) blocks on A. With PIP the donation propagates to L, " +
				"which runs at priority 3 despite M(prio2) released at t4. " +
				"Without PIP, M preempts L and the whole chain stalls, roughly " +
				"doubling H's blocking time.",
			Tasks: []TaskSpec{
				{Name: "L", Priority: 1, ReleaseTime: 0, Steps: []Step{
					{Op: OpLock, Resource: "B"},
					{Op: OpCompute, Duration: 6},
					{Op: OpUnlock, Resource: "B"},
					{Op: OpCompute, Duration: 1},
				}},
				{Name: "X", Priority: 2, ReleaseTime: 1, Steps: []Step{
					{Op: OpLock, Resource: "A"},
					{Op: OpCompute, Duration: 2},
					{Op: OpLock, Resource: "B"},
					{Op: OpCompute, Duration: 2},
					{Op: OpUnlock, Resource: "B"},
					{Op: OpUnlock, Resource: "A"},
					{Op: OpCompute, Duration: 1},
				}},
				{Name: "H", Priority: 3, ReleaseTime: 3, Steps: []Step{
					{Op: OpLock, Resource: "A"},
					{Op: OpCompute, Duration: 2},
					{Op: OpUnlock, Resource: "A"},
				}},
				{Name: "M", Priority: 2, ReleaseTime: 4, Steps: []Step{
					{Op: OpCompute, Duration: 5},
				}},
			},
		},
		{
			ID:   "deadlock",
			Name: "Deadlock is detected but NOT prevented by inheritance",
			Description: "Locks taken in opposite orders create a wait-for " +
				"cycle. Priority inheritance bounds inversion but cannot break " +
				"deadlock; the simulator detects the cycle and reports it.",
			Narrative: "L(prio1) holds A and requests B; H(prio3) holds B and " +
				"requests A. Both runs (PIP on/off) end with a DEADLOCK event " +
				"naming the cycle, which is the correct behavior: PIP is not a " +
				"deadlock-prevention protocol.",
			Tasks: []TaskSpec{
				{Name: "L", Priority: 1, ReleaseTime: 0, Steps: []Step{
					{Op: OpLock, Resource: "A"},
					{Op: OpCompute, Duration: 3},
					{Op: OpLock, Resource: "B"},
					{Op: OpCompute, Duration: 1},
					{Op: OpUnlock, Resource: "B"},
					{Op: OpUnlock, Resource: "A"},
				}},
				{Name: "H", Priority: 3, ReleaseTime: 3, Steps: []Step{
					{Op: OpLock, Resource: "B"},
					{Op: OpCompute, Duration: 1},
					{Op: OpLock, Resource: "A"},
					{Op: OpCompute, Duration: 1},
					{Op: OpUnlock, Resource: "A"},
					{Op: OpUnlock, Resource: "B"},
				}},
			},
		},
	}
}

// FindScenario returns the built-in scenario with the given id.
func FindScenario(id string) (Scenario, bool) {
	for _, s := range Scenarios() {
		if s.ID == id {
			return s, true
		}
	}
	return Scenario{}, false
}
