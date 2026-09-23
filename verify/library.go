package verify

// Hand-authored short fault traces covering the required fault classes:
// partitions, restarts, and delayed old messages. Each scenario interleaves
// the fault with proposals and virtual-time windows sized to let elections and
// replication settle deterministically.

const settleMs = 220 // > one jittered election timeout + RPC round trips
const replMs = 40    // a few network round trips for replication

// LibraryScenarios returns the curated regression set for one variant.
func LibraryScenarios(variant string) []Scenario {
	mk := func(name string, actions []Action) Scenario {
		return Scenario{
			Name: name, Nodes: 3, Variant: variant, Seed: 99, Actions: actions,
		}
	}
	return []Scenario{
		mk("happy-path", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			{Kind: KindProposeAll, Command: "SET a b"},
			{Kind: KindAdvance, AdvanceMs: replMs},
		}),
		mk("leader-partition-heal", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			// Partition the current leader (filled in dynamically is not
			// possible in a static scenario; instead isolate node 1, which is
			// the deterministic first leader under seed 99).
			{Kind: KindPartition, Peers: []int{1}},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v2"},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindHeal},
			{Kind: KindAdvance, AdvanceMs: settleMs},
		}),
		mk("minority-partition-new-term", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET x 1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			{Kind: KindPartition, Peers: []int{3}},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindHeal},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET x 2"},
			{Kind: KindAdvance, AdvanceMs: replMs},
		}),
		mk("follower-restart", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			{Kind: KindRestart, Node: 3},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v2"},
			{Kind: KindAdvance, AdvanceMs: replMs},
		}),
		mk("leader-restart", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			{Kind: KindRestart, Node: 1},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v2"},
			{Kind: KindAdvance, AdvanceMs: settleMs},
		}),
		mk("stale-message-delay", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			// Hold node 3's messages, let it fall behind, then rejoin and
			// release the stale queue.
			{Kind: KindPause, Node: 3},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v2"},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindResume, Node: 3},
			{Kind: KindAdvance, AdvanceMs: settleMs},
		}),
		mk("partition-pause-stale-old-leader", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			// Isolate the first leader but keep its inbound queue flowing into
			// a hold so its stale entries are released post-recovery.
			{Kind: KindPause, Node: 1},
			{Kind: KindPartition, Peers: []int{1}},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v2"},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindHeal},
			{Kind: KindResume, Node: 1},
			{Kind: KindAdvance, AdvanceMs: settleMs},
		}),
		mk("split-then-restart-quorum", []Action{
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			{Kind: KindPartition, Peers: []int{1}},
			{Kind: KindRestart, Node: 2},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindHeal},
			{Kind: KindAdvance, AdvanceMs: settleMs},
		}),
		mk("five-node-chaos", []Action{
			// Nodes is patched to 5 by FiveNodeScenarios.
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v1"},
			{Kind: KindAdvance, AdvanceMs: replMs},
			{Kind: KindPartition, Peers: []int{4, 5}},
			{Kind: KindRestart, Node: 3},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindHeal},
			{Kind: KindPause, Node: 2},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindResume, Node: 2},
			{Kind: KindAdvance, AdvanceMs: settleMs},
			{Kind: KindProposeAll, Command: "SET k v2"},
			{Kind: KindAdvance, AdvanceMs: replMs},
		}),
	}
}
