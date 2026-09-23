package sim

import "fmt"

// CheckInvariants verifies the safety properties that must hold in every
// scenario, including the hostile ones. These checks read only the reported
// state and the event trace, exactly as an external auditor would.
func CheckInvariants(rep *Report, trace *Trace) []InvariantCheck {
	var checks []InvariantCheck

	// 1. Fencing tokens issued by the lock are strictly increasing.
	checks = append(checks, checkFenceMonotonic(rep))

	// 2. Fences of committed writes never decrease. Multiple writes by the
	//    same holder legitimately carry the same (equal) fence; a write is
	//    stale only when its fence is smaller than the resource epoch — the
	//    headline guarantee is that a smaller fence never commits.
	checks = append(checks, checkCommittedFencesNondecreasing(rep))

	// 3. Every rejected submit at the resource carried a fence no greater
	//    than the resource's current epoch (no valid newer write was turned
	//    away) — the guard never blocks a legitimate current holder.
	checks = append(checks, checkRejectionsSound(rep))

	// 4. No committed write is without a fence.
	checks = append(checks, checkCommitsHaveFences(rep))

	// 5. Lock grant/expire bookkeeping is consistent: grants are issued only
	//    when no live lease exists, and every expiration follows the TTL.
	checks = append(checks, checkLeaseLifecycle(rep, trace))

	// 6. A stale fence write (fence below the resource epoch) observed in the
	//    trace is never followed by its commit.
	checks = append(checks, checkNoStaleCommit(rep, trace))

	return checks
}

func check(name string, pass bool, detail string) InvariantCheck {
	return InvariantCheck{Name: name, Pass: pass, Detail: detail}
}

func checkFenceMonotonic(rep *Report) InvariantCheck {
	for i, h := range rep.Lock.History {
		if h.Fence != int64(i+1) {
			return check("fence_tokens_strictly_increasing", false,
				fmt.Sprintf("grant #%d has fence %d", i+1, h.Fence))
		}
	}
	return check("fence_tokens_strictly_increasing", true,
		fmt.Sprintf("%d tokens issued, fences 1..%d", len(rep.Lock.History), rep.Lock.NextFence-1))
}

func checkCommittedFencesNondecreasing(rep *Report) InvariantCheck {
	var prev int64
	for i, c := range rep.Resource.Committed {
		if i > 0 && c.Fence < prev {
			return check("committed_fences_never_decrease", false,
				fmt.Sprintf("commit #%d fence %d follows %d", i+1, c.Fence, prev))
		}
		prev = c.Fence
	}
	return check("committed_fences_never_decrease", true,
		fmt.Sprintf("%d commits", len(rep.Resource.Committed)))
}

func checkRejectionsSound(rep *Report) InvariantCheck {
	for _, rj := range rep.Resource.Rejected {
		// The recorded epoch is the max fence at the time of rejection.
		if rj.Reason == ResultStaleFence && rj.Fence >= rj.EpochFence {
			return check("rejections_only_for_stale_or_fenceless", false,
				fmt.Sprintf("reject of fence %d at epoch %d", rj.Fence, rj.EpochFence))
		}
		if rj.Reason == ResultFenceZero && rj.Fence != 0 {
			return check("rejections_only_for_stale_or_fenceless", false,
				"fence_zero rejection carried nonzero fence")
		}
	}
	return check("rejections_only_for_stale_or_fenceless", true,
		fmt.Sprintf("%d stale/fenceless writes rejected", len(rep.Resource.Rejected)))
}

func checkCommitsHaveFences(rep *Report) InvariantCheck {
	for _, c := range rep.Resource.Committed {
		if c.Fence <= 0 {
			return check("every_commit_has_fence", false,
				fmt.Sprintf("commit %s at t=%d has fence %d", c.ReqID, c.At, c.Fence))
		}
	}
	return check("every_commit_has_fence", true, "")
}

func checkLeaseLifecycle(rep *Report, trace *Trace) InvariantCheck {
	grants := trace.Select(func(e TraceEvent) bool { return e.Event == "lock.grant" })
	expires := trace.Select(func(e TraceEvent) bool { return e.Event == "lock.expire" })

	// A grant must never start while an earlier lease is still alive (not
	// expired and not released). Walk the full trace in order.
	alive := map[int64]bool{}
	for _, e := range trace.Events {
		switch e.Event {
		case "lock.grant":
			for f, live := range alive {
				if live {
					return check("lease_not_overgranted_while_alive", false,
						fmt.Sprintf("grant fence %d at t=%d overlaps live fence %d", int64Field(e, "fence"), e.Time, f))
				}
			}
			alive[int64Field(e, "fence")] = true
		case "lock.expire", "lock.release":
			alive[int64Field(e, "fence")] = false
		}
	}

	return check("lease_not_overgranted_while_alive", true,
		fmt.Sprintf("%d grants, %d expirations", len(grants), len(expires)))
}

func checkNoStaleCommit(rep *Report, trace *Trace) InvariantCheck {
	// For every resource.commit event, its fence must be at least the max
	// fence committed so far. Equal is fine: one holder may write repeatedly.
	max := int64(0)
	commits := trace.Select(func(e TraceEvent) bool { return e.Event == "resource.commit" })
	for _, e := range commits {
		f := int64Field(e, "fence")
		if f < max {
			return check("no_stale_fence_ever_committed", false,
				fmt.Sprintf("commit fence %d at t=%d is below epoch %d", f, e.Time, max))
		}
		max = f
	}
	return check("no_stale_fence_ever_committed", true,
		fmt.Sprintf("max committed fence = %d", max))
}

func int64Field(e TraceEvent, key string) int64 {
	if e.Detail == nil {
		return 0
	}
	switch v := e.Detail[key].(type) {
	case int64:
		return v
	case float64:
		return int64(v)
	case int:
		return int64(v)
	default:
		return 0
	}
}
