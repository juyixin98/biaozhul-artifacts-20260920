// Package gc implements coordinated, safe reclamation of OR-Set tombstones.
//
// When a tombstone tag may be deleted:
//
// A tombstone for tag T can be physically removed on replica R only once R is
// certain that NO surviving replica can ever send T as a "live add" again.
// That requires:
//
//  1. Every surviving replica has observed T in both its add history and its
//     tombstone set (so every replica already resolves T as dead). A replica
//     that has never seen the add could otherwise receive it later and
//     resurrect the element.
//  2. Replicas that are permanently gone (decommissioned, disks wiped) are
//     excluded from the quorum. A merely offline/slow replica MUST still be
//     counted: its queued updates may contain T.
//
// Therefore GC is a cluster-wide coordinated decision, not a local one. This
// package computes the intersection of "known-dead tags" across the current
// states of all live replicas; only tags in that intersection are eligible.
// The purge itself is idempotent, so re-running GC (e.g. after a failed node
// call) is harmless.
package gc

import (
	"context"
	"sort"

	"orset/internal/orset"
)

// Report describes one GC run.
type Report struct {
	// Eligible maps element -> tags every surveyed replica knows as dead.
	Eligible map[string][]string
	// TagCount is the total number of tags purged (sum over replicas).
	TagCount int
	// Purged[i] is how many tags replica i purged.
	Purged []int
	// Replicas is the base URL of every replica that was coordinated.
	Replicas []string
}

// Client is the transport a coordinator uses to talk to replicas.
// The real implementation is internal/server.GCClient; tests supply fakes.
type Client interface {
	// State fetches the replica's current full state.
	State(ctx context.Context) (orset.State, error)
	// Purge asks the replica to physically delete the given tags.
	Purge(ctx context.Context, tags map[string][]string) (int, error)
}

// Eligible returns tags known dead by EVERY given state (intersection of the
// tombstone sets). Tags must also be present in the add history of every
// state — a tag only seen in a tombstone set without its add is harmless but
// requiring the add to be observed too is the conservative condition that
// protects a replica that has not received the original add message.
func Eligible(states []orset.State) map[string][]string {
	if len(states) == 0 {
		return nil
	}
	// dead everywhere
	dead := map[string]map[string]int{}
	for _, st := range states {
		for e, tags := range st.Tombstones {
			m := dead[e]
			if m == nil {
				m = map[string]int{}
				dead[e] = m
			}
			for _, t := range tags {
				m[t]++
			}
		}
	}
	n := len(states)
	out := map[string][]string{}
	for e, tags := range dead {
		for t, c := range tags {
			if c != n {
				continue
			}
			// conservative: the add must also be observed everywhere
			observedAddEverywhere := true
			for _, st := range states {
				found := false
				for _, at := range st.Add[e] {
					if at == t {
						found = true
						break
					}
				}
				if !found {
					observedAddEverywhere = false
					break
				}
			}
			if observedAddEverywhere {
				out[e] = append(out[e], t)
			}
		}
	}
	for e := range out {
		sort.Strings(out[e])
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// Coordinate runs one safe GC round over clients: fetch every replica's
// state, compute the intersection of safe tags, and purge them everywhere.
// If ANY state fetch fails the run aborts BEFORE purging anything — better
// to keep tombstones than to risk a resurrection (fail closed). Purge calls
// are idempotent, so a partial purge failure may simply be retried.
func Coordinate(ctx context.Context, clients []Client, replicas []string) (*Report, error) {
	states := make([]orset.State, len(clients))
	for i, c := range clients {
		st, err := c.State(ctx)
		if err != nil {
			return nil, fmtErrf(replicas, i, "fetch state: %w", err)
		}
		states[i] = st
	}

	eligible := Eligible(states)
	rep := &Report{Eligible: eligible, Replicas: append([]string(nil), replicas...)}
	rep.Purged = make([]int, len(clients))
	if len(eligible) == 0 {
		return rep, nil
	}

	for i, c := range clients {
		n, err := c.Purge(ctx, eligible)
		if err != nil {
			return rep, fmtErrf(replicas, i, "purge: %w", err)
		}
		rep.Purged[i] = n
		rep.TagCount += n
	}
	return rep, nil
}
