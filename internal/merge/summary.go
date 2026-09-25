package merge

import (
	"sort"

	"github.com/local/testmerge/internal/domain"
)

// AttemptSummary describes one attempt in the final report.
type AttemptSummary struct {
	AttemptID    string `json:"attempt_id"`
	Shard        string `json:"shard"`
	AttemptNo    int    `json:"attempt_no"`
	Status       string `json:"status"` // passed|failed|cancelled|incomplete
	Message      string `json:"message,omitempty"`
	Started      bool   `json:"started"`
	Finished     bool   `json:"finished"`
	Latest       bool   `json:"latest"`
	OrphanFinish bool   `json:"orphan_finish,omitempty"`
	LateWrites   int    `json:"late_writes"`
	DupStarts    int    `json:"dup_starts,omitempty"`
	StartSeq     int64  `json:"start_seq,omitempty"`
	FinishSeq    int64  `json:"finish_seq,omitempty"`
}

// TestSummary describes one test and all of its attempts.
type TestSummary struct {
	TestID          string            `json:"test_id"`
	Status          domain.TestStatus `json:"status"`
	LatestAttemptID string            `json:"latest_attempt_id,omitempty"`
	LatestStatus    string            `json:"latest_status"` // incl. incomplete
	Reason          string            `json:"reason"`
	Attempts        []AttemptSummary  `json:"attempts"`
}

// ShardSummary is one shard's lifecycle state.
type ShardSummary struct {
	Shard    string `json:"shard"`
	Started  bool   `json:"started"`
	Finished bool   `json:"finished"`
	Status   string `json:"status"` // finished|incomplete
}

// Counts is a status -> count table for the four explicit states.
type Counts struct {
	Passed     int `json:"passed"`
	Failed     int `json:"failed"`
	Cancelled  int `json:"cancelled"`
	Incomplete int `json:"incomplete"`
}

// RunSummary is the full, repeatable merge result.
type RunSummary struct {
	RunID     string           `json:"run_id"`
	Status    domain.RunStatus `json:"status"`
	Counts    Counts           `json:"counts"`
	Total     int              `json:"total"`
	Tests     []TestSummary    `json:"tests"`
	Shards    []ShardSummary   `json:"shards"`
	Started   bool             `json:"started"`
	Finished  bool             `json:"finished"`
	Cancelled bool             `json:"cancelled"`
	// MissingFinish is true when run_finished never arrived (executor crash)
	// but results exist. Such a run can never be "passed".
	MissingFinish bool `json:"missing_finish"`
	EventsApplied int  `json:"events_applied"`
	LateWrites    int  `json:"late_writes_total"`
}

// Summarize derives the full report from a reduced run. It is a pure
// function of the event log and therefore repeatable: replaying the same
// events in any order yields an identical summary.
func (r *Run) Summarize() RunSummary {
	out := RunSummary{
		RunID:     r.RunID,
		Started:   r.started,
		Finished:  r.finished,
		Cancelled: r.cancelled,
		// Any observed activity without run_finished means the executor
		// died (possibly before emitting run_started), so a pass cannot be
		// claimed. A run with only run_started is likewise unfinished.
		MissingFinish: !r.finished && (r.started || len(r.attempts) > 0 || len(r.shards) > 0),
	}

	testIDs := make([]string, 0, len(r.tests))
	for t := range r.tests {
		testIDs = append(testIDs, t)
	}
	sort.Strings(testIDs)

	for _, tid := range testIDs {
		ids := r.testOrder[tid]
		atts := make([]*attempt, 0, len(ids))
		for _, id := range ids {
			if a, ok := r.attempts[attemptKey(tid, id)]; ok {
				atts = append(atts, a)
			}
		}
		if len(atts) == 0 {
			// Declared but never observed: incomplete, never passed.
			out.Tests = append(out.Tests, TestSummary{
				TestID:       tid,
				Status:       domain.TestIncomplete,
				LatestStatus: string(domain.TestIncomplete),
				Reason:       "declared but no attempt observed",
				Attempts:     []AttemptSummary{},
			})
			continue
		}

		latest := latestAttempt(atts)
		ts := TestSummary{TestID: tid}
		for _, a := range atts {
			status := string(domain.TestIncomplete)
			if a.status != "" {
				status = string(a.status)
			}
			asum := AttemptSummary{
				AttemptID:  a.attemptID,
				Shard:      a.shard,
				AttemptNo:  a.no,
				Status:     status,
				Message:    a.message,
				Started:    a.startSeen,
				Finished:   a.finishedSeq >= 0,
				Latest:     a == latest,
				LateWrites: a.lateWrites,
				DupStarts:  a.dupStarts,
				StartSeq:   a.startedSeq,
				FinishSeq:  a.finishedSeq,
			}
			if a.finishedSeq >= 0 && !a.startSeen {
				asum.OrphanFinish = true
			}
			ts.Attempts = append(ts.Attempts, asum)
			out.LateWrites += a.lateWrites
		}

		ts.LatestAttemptID = latest.attemptID
		if latest.status == "" {
			ts.LatestStatus = string(domain.TestIncomplete)
		} else {
			ts.LatestStatus = string(latest.status)
		}
		ts.Status, ts.Reason = effectiveTestStatus(latest, r.cancelled)
		out.Tests = append(out.Tests, ts)
	}

	for _, t := range out.Tests {
		switch t.Status {
		case domain.TestPassed:
			out.Counts.Passed++
		case domain.TestFailed:
			out.Counts.Failed++
		case domain.TestCancelled:
			out.Counts.Cancelled++
		default:
			out.Counts.Incomplete++
		}
	}
	out.Total = len(out.Tests)

	shardNames := make([]string, 0, len(r.shards))
	for name := range r.shards {
		shardNames = append(shardNames, name)
	}
	sort.Strings(shardNames)
	for _, name := range shardNames {
		s := r.shards[name]
		st := "incomplete"
		if s.finished {
			st = "finished"
		}
		out.Shards = append(out.Shards, ShardSummary{
			Shard: name, Started: s.started, Finished: s.finished, Status: st,
		})
	}

	out.EventsApplied = len(r.eventIDs)
	out.Status = runStatus(out.Counts, out.Cancelled, out.MissingFinish, r.finishStatus, out.Total)
	return out
}

// latestAttempt picks the attempt that determines the test outcome:
// highest AttemptNo, ties broken by first observation order (which is
// canonicalized by seq order in Reduce).
func latestAttempt(atts []*attempt) *attempt {
	best := atts[0]
	for _, a := range atts[1:] {
		if a.no != best.no {
			if a.no > best.no {
				best = a
			}
			continue
		}
		if a.seenOrder > best.seenOrder {
			best = a
		}
	}
	return best
}

// effectiveTestStatus derives the four-state status from the LATEST attempt
// only. A late/duplicated terminal result written to an older attempt can
// therefore never overwrite the current outcome.
func effectiveTestStatus(latest *attempt, runCancelled bool) (domain.TestStatus, string) {
	switch latest.status {
	case domain.AttemptPassed:
		return domain.TestPassed, "latest attempt passed"
	case domain.AttemptFailed:
		return domain.TestFailed, "latest attempt failed"
	case domain.AttemptCancelled:
		return domain.TestCancelled, "latest attempt cancelled"
	default:
		if runCancelled {
			return domain.TestCancelled, "run cancelled before latest attempt finished"
		}
		return domain.TestIncomplete, "latest attempt has no terminal result"
	}
}

// runStatus aggregates test states. An explicit executor-reported status is
// considered but can never hide failures/incomplete results.
func runStatus(c Counts, cancelled, missingFinish bool, reported domain.RunStatus, total int) domain.RunStatus {
	switch {
	case c.Failed > 0:
		return domain.RunFailed
	case c.Incomplete > 0:
		return domain.RunIncomplete
	case c.Cancelled > 0:
		// Explicit cancellation of some tests with the rest passing.
		return domain.RunCancelled
	}
	if cancelled {
		return domain.RunCancelled
	}
	if missingFinish {
		// Executor crashed: even if every observed result passed, without a
		// run_finished event the run is incomplete, never passed.
		return domain.RunIncomplete
	}
	if reported != "" {
		switch reported {
		case domain.RunFailed, domain.RunIncomplete, domain.RunCancelled, domain.RunPassed:
			return reported
		}
	}
	if total == 0 {
		// Nothing declared and nothing observed is an empty/incomplete run;
		// calling an empty run "passed" would silently hide missing results.
		return domain.RunIncomplete
	}
	return domain.RunPassed
}
