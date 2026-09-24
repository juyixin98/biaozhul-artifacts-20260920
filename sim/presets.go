package sim

// Preset returns a fresh copy of one of the built-in acceptance workloads:
//
//   - "basic":  classic three-task priority inversion. Low (prio 10) holds R;
//     at t=3 High (prio 30) wants R and blocks; meanwhile Medium (prio 20,
//     released t=4) would interpose and delay High without inheritance.
//   - "nested": nested lock chain with transitive inheritance. Low holds R1;
//     Medium takes R2 and then blocks on R1 while keeping R2; at the same
//     instant High blocks on R2. Effective priority must propagate
//     High -> Medium -> Low (Low is boosted straight to 30) until the chain
//     unwinds. A separate Busy task (prio 15) interposes without inheritance.
//
// Priorities: larger number == higher priority.
func Preset(name string) (Workload, bool) {
	switch name {
	case "basic":
		return Workload{
			Name:      "basic-priority-inversion",
			Resources: []string{"R"},
			Tasks: []TaskSpec{
				{
					ID: "Low", BasePriority: 10, Release: 0,
					Ops: []Op{
						{Kind: OpLock, Resource: "R"},
						{Kind: OpCompute, Duration: 6},
						{Kind: OpUnlock, Resource: "R"},
						{Kind: OpCompute, Duration: 3},
					},
				},
				{
					ID: "Medium", BasePriority: 20, Release: 4,
					Ops: []Op{
						{Kind: OpCompute, Duration: 4},
					},
				},
				{
					ID: "High", BasePriority: 30, Release: 3,
					Ops: []Op{
						{Kind: OpLock, Resource: "R"},
						{Kind: OpCompute, Duration: 3},
						{Kind: OpUnlock, Resource: "R"},
					},
				},
			},
		}, true
	case "nested":
		return Workload{
			Name:      "nested-lock-chain",
			Resources: []string{"R1", "R2"},
			Tasks: []TaskSpec{
				{
					ID: "Low", BasePriority: 10, Release: 0,
					Ops: []Op{
						{Kind: OpLock, Resource: "R1"},
						{Kind: OpCompute, Duration: 6}, // [0,2) then boosted [4,8)
						{Kind: OpUnlock, Resource: "R1"},
						{Kind: OpCompute, Duration: 4}, // own work after the chain
					},
				},
				{
					ID: "Medium", BasePriority: 20, Release: 2,
					Ops: []Op{
						{Kind: OpLock, Resource: "R2"},
						{Kind: OpCompute, Duration: 2}, // holds R2
						{Kind: OpLock, Resource: "R1"}, // blocks at t=4: Low owns R1, Medium keeps R2
						{Kind: OpCompute, Duration: 2},
						{Kind: OpUnlock, Resource: "R1"},
						{Kind: OpCompute, Duration: 1},
						{Kind: OpUnlock, Resource: "R2"},
						{Kind: OpCompute, Duration: 2},
					},
				},
				{
					ID: "High", BasePriority: 30, Release: 4,
					Ops: []Op{
						{Kind: OpLock, Resource: "R2"}, // same settle: High waits R2 (Medium), Medium waits R1 (Low)
						{Kind: OpCompute, Duration: 3},
						{Kind: OpUnlock, Resource: "R2"},
					},
				},
				{
					ID: "Busy", BasePriority: 15, Release: 3,
					Ops: []Op{
						{Kind: OpCompute, Duration: 4}, // plain interposer
					},
				},
			},
		}, true
	default:
		return Workload{}, false
	}
}

// PresetNames lists the available built-in workloads.
func PresetNames() []string {
	return []string{"basic", "nested"}
}
