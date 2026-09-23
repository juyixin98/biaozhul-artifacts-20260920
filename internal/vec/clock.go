// Package vec implements vector clocks keyed by logical (origin node, sequence)
// pairs used as causal dependencies.
package vec

import (
	"fmt"
	"sort"
)

// Key identifies one broadcast message: the node that originated it and its
// per-origin monotonically increasing sequence number (1-based).
type Key struct {
	Node string `json:"node"`
	Seq  int    `json:"seq"`
}

// String renders a key as node:seq.
func (k Key) String() string {
	return fmt.Sprintf("%s:%d", k.Node, k.Seq)
}

// Less imposes a deterministic total order on keys (node id, then seq).
func (k Key) Less(o Key) bool {
	if k.Node != o.Node {
		return k.Node < o.Node
	}
	return k.Seq < o.Seq
}

// Clock is a vector clock: origin node -> largest sequence seen from it.
type Clock map[string]int

// Clone returns a deep copy.
func (c Clock) Clone() Clock {
	out := make(Clock, len(c))
	for k, v := range c {
		out[k] = v
	}
	return out
}

// Has reports whether dependency k is satisfied (clock[k.Node] >= k.Seq).
func (c Clock) Has(k Key) bool {
	return c[k.Node] >= k.Seq
}

// Observe advances the clock to include key k (max, not just +1, so it is
// safe even with gaps caused by permanent loss).
func (c Clock) Observe(k Key) {
	if c[k.Node] < k.Seq {
		c[k.Node] = k.Seq
	}
}

// Merge folds another clock into c.
func (c Clock) Merge(o Clock) {
	for k, v := range o {
		if v > c[k] {
			c[k] = v
		}
	}
}

// Deps converts a clock into the explicit list of every (node, seq) pair it
// covers. Used as the dependency vector attached to new messages.
func (c Clock) Deps() []Key {
	var out []Key
	nodes := make([]string, 0, len(c))
	for n := range c {
		nodes = append(nodes, n)
	}
	sort.Strings(nodes)
	for _, n := range nodes {
		for s := 1; s <= c[n]; s++ {
			out = append(out, Key{Node: n, Seq: s})
		}
	}
	return out
}

// MissingDeps returns the dependencies required by deps that the clock does
// not yet satisfy, in deterministic order.
func MissingDeps(clock Clock, deps []Key) []Key {
	var missing []Key
	for _, k := range deps {
		if !clock.Has(k) {
			missing = append(missing, k)
		}
	}
	sort.Slice(missing, func(i, j int) bool { return missing[i].Less(missing[j]) })
	return missing
}
