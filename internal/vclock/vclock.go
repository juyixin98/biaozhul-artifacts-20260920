// Package vclock implements a vector clock keyed by replica ID.
//
// A clock maps each replica to the number of events it has knowledge of from
// that replica. Clocks are compared by the standard happens-before relation:
//
//	a <= b  iff  every component of a is <= the corresponding component of b.
package vclock

import (
	"sort"
	"strconv"
	"strings"
)

// Clock is a vector clock. The zero value (nil/empty map) is valid and
// represents the initial clock before any event.
type Clock map[string]int64

// Relation describes how two clocks relate under happens-before.
type Relation int

const (
	// Equal means the clocks have identical components.
	Equal Relation = iota
	// Before means a happens-before b (a is causal history of b).
	Before
	// After means b happens-before a.
	After
	// Concurrent means neither clock dominates the other.
	Concurrent
)

func (r Relation) String() string {
	switch r {
	case Equal:
		return "equal"
	case Before:
		return "before"
	case After:
		return "after"
	default:
		return "concurrent"
	}
}

// Get returns the component for id, or 0 if absent.
func (c Clock) Get(id string) int64 { return c[id] }

// Copy returns an independent copy of c.
func (c Clock) Copy() Clock {
	out := make(Clock, len(c))
	for k, v := range c {
		out[k] = v
	}
	return out
}

// Compare returns how clock a relates to clock b.
func Compare(a, b Clock) Relation {
	aLess, bLess := false, false
	for id := range a {
		av, bv := a[id], b[id]
		if av < bv {
			aLess = true
		} else if av > bv {
			bLess = true
		}
	}
	for id := range b {
		if _, ok := a[id]; ok {
			continue
		}
		if 0 < b[id] {
			aLess = true
		}
	}
	switch {
	case aLess && bLess:
		return Concurrent
	case aLess:
		return Before
	case bLess:
		return After
	default:
		return Equal
	}
}

// Join returns the component-wise maximum (least upper bound) of a and b.
// Neither input is modified.
func Join(a, b Clock) Clock {
	out := a.Copy()
	for id, v := range b {
		if v > out[id] {
			out[id] = v
		}
	}
	return out
}

// Increment returns a copy of c with the component for id advanced by one.
func Increment(c Clock, id string) Clock {
	out := c.Copy()
	out[id]++
	return out
}

// Canonical renders the clock as a deterministic string, suitable for use as
// a map key or for equality checks. Components are emitted in replica-ID
// order and zero components are skipped.
func (c Clock) Canonical() string {
	ids := make([]string, 0, len(c))
	for id, v := range c {
		if v != 0 {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	var b strings.Builder
	for _, id := range ids {
		b.WriteString(id)
		b.WriteByte('=')
		b.WriteString(strconv.FormatInt(c[id], 10))
		b.WriteByte(',')
	}
	return b.String()
}
