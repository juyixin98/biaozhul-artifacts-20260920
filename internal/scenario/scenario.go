// Package scenario builds the built-in demonstration requests: a causal
// chain (every broadcast depends on the previous one) and a concurrent
// broadcast burst (independent messages arriving out of order).
package scenario

import (
	"fmt"

	"causal-broadcast/internal/jsonio"
)

// Chain builds a causal-chain request.
//
// With n nodes, node A broadcasts at time 0; every other node broadcasts
// once in round-robin order shortly after. The timing makes each broadcast
// depend causally on the previous one, so out-of-order links force
// buffering along the chain.
//
// If lossLast is true, the penultimate chain message is force-dropped at
// the final node, so the final message is permanently blocked there and the
// diagnostics report a root-cause chain.
func Chain(n int, seed int64, lossLast bool) jsonio.Request {
	if n < 2 {
		n = 2
	}
	names := nodeNames(n)
	req := jsonio.Request{
		Nodes:     names,
		BufferCap: 1024,
		Network: jsonio.NetworkConfig{
			Seed: ptr(seed),
			// Steps are 1.5 apart while delays range up to ~2.5, so a
			// successor can reach a node before its predecessor and be held.
			Default: &jsonio.LinkJSON{
				BaseDelay:  ptr(1.0),
				Jitter:     ptr(0.9),
				ReorderJit: ptr(0.6),
			},
		},
	}
	for i := 0; i < n; i++ {
		req.Broadcasts = append(req.Broadcasts, jsonio.BroadcastJSON{
			Time: float64(i) * 2.0,
			From: names[i%n],
			Body: fmt.Sprintf("chain-step-%d", i+1),
		})
	}
	// Scripted out-of-order delivery: each predecessor message is delayed to
	// the node two hops away, so the successor arrives first and is buffered
	// until the predecessor catches up. This is independent of the RNG seed.
	for i := 0; i+2 < n; i++ {
		req.Network.DelayedIDs = append(req.Network.DelayedIDs, jsonio.HoldEntry{
			MsgID: names[i] + "1",
			Dst:   []string{names[i+2]},
			Delay: jsonio.FlexFloat{float64(i)*2.0 + 3.0},
		})
	}
	if lossLast && n >= 2 {
		last := names[n-1]
		// Drop the earliest predecessor A1 at the final node. Every later
		// chain message that reaches the final node is then blocked: B1 is
		// buffered and its root-cause chain points at A1@<last>; C1/D1 (if
		// they arrive) are reported as never-arrived with the same root.
		req.Network.DropIDs = append(req.Network.DropIDs, jsonio.DropEntry{
			MsgID: names[0] + "1",
			Dst:   []string{last},
		})
	}
	return req
}

// Concurrent builds an independent-burst request: every node broadcasts
// once in a short window, so the messages carry no dependencies on one
// another. With jittered links they arrive out of order but must all
// deliver immediately (no causal buffering between independent messages);
// a scripted late duplicate copy is suppressed.
func Concurrent(n int, seed int64, duplicate bool) jsonio.Request {
	if n < 2 {
		n = 2
	}
	names := nodeNames(n)
	req := jsonio.Request{
		Nodes:     names,
		BufferCap: 1024,
		Network: jsonio.NetworkConfig{
			Seed: ptr(seed),
			// One broadcast per node only: even if jitter reorders copies of
			// different messages, none depends on another, so nothing may
			// buffer. (Avoiding a second message per node also removes
			// same-sender reordering, which would legitimately buffer.)
			Default: &jsonio.LinkJSON{
				BaseDelay:  ptr(3.0),
				Jitter:     ptr(1.0),
				ReorderJit: ptr(1.0),
			},
		},
	}
	t := 0.0
	for i := 0; i < n; i++ {
		req.Broadcasts = append(req.Broadcasts, jsonio.BroadcastJSON{
			Time: t,
			From: names[i],
			Body: fmt.Sprintf("concurrent-node-%s", names[i]),
		})
		t += 0.05
	}
	if duplicate {
		// Force an extra late copy of the first broadcast to every node, so
		// duplicate suppression is always demonstrable regardless of RNG.
		req.Network.HoldIDs = append(req.Network.HoldIDs, jsonio.HoldEntry{
			MsgID: names[0] + "1",
			Delay: flex(20),
		})
	}
	return req
}

// flex is a tiny helper to build the flex float list.
func flex(x float64) jsonio.FlexFloat { return jsonio.FlexFloat{x} }

func nodeNames(n int) []string {
	out := make([]string, n)
	for i := 0; i < n; i++ {
		out[i] = string(rune('A' + i))
	}
	return out
}

func ptr[T any](v T) *T { return &v }
