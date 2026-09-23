package sim

import "sort"

// verify runs the final acceptance checks after the event loop drains:
//
//  1. ownership — every key with a confirmed write lives on its final-ring
//     owner, at exactly the highest confirmed version and value;
//  2. staleness — no read returned data older than a write confirmed before
//     the read was issued (collected during the run);
//  3. safe removal — no decommissioned node was the sole holder of a newer
//     version (checked at decommission time);
//  4. liveness — every issued write/read was answered (no failed ops).
func (s *Sim) verify() {
	v := &s.result.Verification
	v.KeyIssues = []KeyIssue{}
	v.StaleReads = s.staleIssues
	if v.StaleReads == nil {
		v.StaleReads = []ReadIssue{}
	}
	v.RemovalViolations = s.removalIssues
	if v.RemovalViolations == nil {
		v.RemovalViolations = []RemovalIssue{}
	}
	v.FailedOps = s.result.Stats.WritesFailed + s.result.Stats.ReadsFailed

	keys := make([]string, 0, len(s.confirmed))
	for k := range s.confirmed {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	v.KeysChecked = len(keys)

	for _, k := range keys {
		want := s.confirmed[k]
		owner := s.router.ring.Owner(k)
		n := s.nodes[owner]
		if n == nil {
			v.KeyIssues = append(v.KeyIssues, KeyIssue{
				Key: k, FinalOwner: owner, WantVersion: want.Version,
				Detail: "final owner not present in cluster",
			})
			continue
		}
		got, ok := n.store[k]
		if !ok {
			v.KeyIssues = append(v.KeyIssues, KeyIssue{
				Key: k, FinalOwner: owner, WantVersion: want.Version,
				Detail: "final owner holds no copy",
			})
			continue
		}
		if got.Version != want.Version || got.Value != want.Value {
			v.KeyIssues = append(v.KeyIssues, KeyIssue{
				Key: k, FinalOwner: owner, WantVersion: want.Version, GotVersion: got.Version,
				Detail: "version/value mismatch on final owner",
			})
		}
	}

	s.result.Stats.UniqueKeysMigrated = len(s.migratedKeys)

	v.Pass = len(v.KeyIssues) == 0 &&
		len(v.StaleReads) == 0 &&
		len(v.RemovalViolations) == 0 &&
		v.FailedOps == 0 &&
		len(s.result.Errors) == 0
}
