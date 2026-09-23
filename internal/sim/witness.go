package sim

// SafetyWitness builds a fully scripted simulator run that realises the given
// disjoint-quorum counterexample: A hears exactly quorumA, B hears exactly
// quorumB, every other transmission is delivered normally. If the quorums are
// genuinely disjoint, both operations complete and the run reports the
// intersection violation end-to-end.
func SafetyWitness(nodes []SimNode, rq, wq int, kind string, quorumA, quorumB []string, failedDomains []string) RunInput {
	setA := map[string]bool{}
	setB := map[string]bool{}
	for _, id := range quorumA {
		setA[id] = true
	}
	for _, id := range quorumB {
		setB[id] = true
	}

	var rules []Rule
	// A-side traffic reaches only A's quorum nodes (both directions).
	for id := range setA {
		rules = append(rules,
			Rule{Role: "A", To: id, MsgType: "request", Action: "deliver", Delay: 1},
			Rule{Role: "A", From: id, MsgType: "response", Action: "deliver", Delay: 1},
		)
	}
	for id := range setB {
		rules = append(rules,
			Rule{Role: "B", To: id, MsgType: "request", Action: "deliver", Delay: 1},
			Rule{Role: "B", From: id, MsgType: "response", Action: "deliver", Delay: 1},
		)
	}
	// Everything else for A/B (requests to outside nodes, their responses) is
	// dropped; later attempts stay dropped too, but the first attempt already
	// completes once a quorum replies.
	rules = append(rules,
		Rule{Role: "A", MsgType: "request", Action: "drop"},
		Rule{Role: "A", MsgType: "response", Action: "drop"},
		Rule{Role: "B", MsgType: "request", Action: "drop"},
		Rule{Role: "B", MsgType: "response", Action: "drop"},
		Rule{Action: "deliver", Delay: 1},
	)

	opKind := "ww"
	if kind == "rw" {
		opKind = "rw"
	}
	return RunInput{
		Nodes:       nodes,
		ReadQuorum:  rq,
		WriteQuorum: wq,
		Sim: Sim{
			Seed:          1,
			Runs:          1,
			Horizon:       30,
			ClientTimeout: 1000, // beyond horizon: witness must not retransmit
			MaxAttempts:   1,
			Operations:    2,
			Kind:          opKind,
			FailedDomains: failedDomains,
			Rules:         rules,
		},
	}
}

// AvailabilityWitness builds a scripted run in which the network is perfect
// but the given failure domains are down. No operation can complete, proving
// the availability failure is caused by the outage rather than packet loss.
func AvailabilityWitness(nodes []SimNode, rq, wq int, kind string, failedDomains []string) RunInput {
	opKind := kind
	if opKind == "" {
		opKind = "ww"
	}
	return RunInput{
		Nodes:       nodes,
		ReadQuorum:  rq,
		WriteQuorum: wq,
		Sim: Sim{
			Seed:          1,
			Runs:          1,
			Horizon:       30,
			ClientTimeout: 1000,
			MaxAttempts:   1,
			Operations:    1,
			Kind:          opKind,
			FailedDomains: failedDomains,
			Rules:         []Rule{{Action: "deliver", Delay: 1}},
		},
	}
}
