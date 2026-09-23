package verify

// Deterministic, hand-built fault traces that falsify buggy protocol
// variants. These are the canonical counterexamples shown and replayed by the
// README and the HTTP demo; the same traces PASS on the standard variant.
//
// The first leader under seed 99 is node 2 (deterministic), which the traces
// rely on explicitly.

func CounterexampleScenarios() []Scenario {
	return []Scenario{
		{
			// Delayed old message: freeze the term-1 leader's *outbound*
			// traffic, partition it away so the majority elects a term-2 leader
			// with an empty log and commits index 1 in term 2, then release the
			// frozen term-1 AppendEntries queue. Standard Raft rejects those
			// messages (term 1 < 2) and the old leader steps down; a variant
			// without the term check accepts them and truncates the committed
			// term-2 entry at index 1.
			Name:    "ce-stale-old-message-overwrites-committed",
			Nodes:   3,
			Variant: "notermcheck",
			Seed:    99,
			Actions: []Action{
				{Kind: KindAdvance, AdvanceMs: settleMs}, // node 2 elected term 1
				{Kind: KindPauseFrom, Node: 2},
				// Old leader proposes locally; its AppendEntries freeze in its
				// outbound queue and never reach the peers.
				{Kind: KindPropose, Node: 2, Command: "SET k OLD"},
				{Kind: KindAdvance, AdvanceMs: replMs},
				{Kind: KindPartition, Peers: []int{2}},
				{Kind: KindAdvance, AdvanceMs: settleMs}, // majority elects term 2
				{Kind: KindProposeAll, Command: "SET k term2"},
				{Kind: KindAdvance, AdvanceMs: replMs}, // term-2 index 1 committed
				{Kind: KindHeal},
				{Kind: KindResumeFrom, Node: 2}, // frozen term-1 AEs land late
				{Kind: KindAdvance, AdvanceMs: settleMs},
			},
		},
		{
			// One-quorum commit: an isolated leader that still reaches one
			// follower commits entries with a replication count of 2 (itself +
			// one). The variant calls that "committed"; after the cluster
			// re-unifies, a majority-backed leader overwrites the prefix, and
			// the two nodes disagree at a committed index.
			Name:    "ce-one-quorum-commit-overwritten",
			Nodes:   3,
			Variant: "naive",
			Seed:    99,
			Actions: []Action{
				{Kind: KindAdvance, AdvanceMs: settleMs}, // node 2 elected term 1
				// Freeze node 2's outbound stream so its committed entry never
				// leaves before the partition (everyone starts from index 0).
				{Kind: KindPauseFrom, Node: 2},
				{Kind: KindPropose, Node: 2, Command: "SET k frozen"},
				{Kind: KindAdvance, AdvanceMs: replMs},
				{Kind: KindPartition, Peers: []int{2}},
				{Kind: KindAdvance, AdvanceMs: settleMs}, // {1,3} elect term 2
				{Kind: KindProposeAll, Command: "SET k majority"},
				{Kind: KindAdvance, AdvanceMs: replMs}, // term-2 index 1 committed on 1&3
				// Node 2, still leader in term 1 with node 3 now reachable as a
				// naive acceptor (no term check), releases its frozen queue and
				// keeps replicating: one-quorum marks its entry committed.
				{Kind: KindHeal},
				{Kind: KindResumeFrom, Node: 2},
				{Kind: KindPropose, Node: 2, Command: "SET k minority"},
				{Kind: KindAdvance, AdvanceMs: settleMs},
			},
		},
	}
}

// StandardCounterpart returns the same trace switched to the standard variant,
// used to show the fix surviving the fault trace.
func StandardCounterpart(sc Scenario) Scenario {
	out := sc
	out.Variant = "standard"
	out.Name = sc.Name + "-on-standard"
	return out
}
