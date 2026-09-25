package trmerge

import "sort"

// AttemptSnapshot describes one attempt of one test.
type AttemptSnapshot struct {
	AttemptID string `json:"attempt_id"`
	AttemptNo int    `json:"attempt_no"`
	ShardID   string `json:"shard_id,omitempty"`
	Started   bool   `json:"started"`
	// Status is the effective status of this attempt: running if it started
	// but never produced a terminal claim, otherwise the conservative
	// precedence over the claim set.
	Status     string   `json:"status"`
	Claims     []string `json:"claims,omitempty"`
	FirstClaim string   `json:"first_claim,omitempty"`
	Conflict   bool     `json:"conflict,omitempty"`
}

// TestSnapshot aggregates all attempts of one logical test.
type TestSnapshot struct {
	TestID string `json:"test_id"`
	Status string `json:"status"` // passed | failed | canceled | incomplete | running
	// LatestAttemptNo is the highest attempt number observed.
	LatestAttemptNo int               `json:"latest_attempt_no"`
	Attempts        []AttemptSnapshot `json:"attempts"`
}

// ShardSnapshot is the executor-level view.
type ShardSnapshot struct {
	ShardID  string   `json:"shard_id"`
	Status   string   `json:"status"` // pending | running | completed | crashed | canceled
	ExitCode *int     `json:"exit_code,omitempty"`
	Error    string   `json:"error,omitempty"`
	Warnings []string `json:"warnings,omitempty"`
	Conflict bool     `json:"conflict,omitempty"`
}

// Conflict lists contradictory terminal claims seen, for forensics.
type Conflict struct {
	Kind   string `json:"kind"` // "attempt" | "shard"
	ID     string `json:"id"`   // attempt_id or shard_id
	TestID string `json:"test_id,omitempty"`
	Claims string `json:"claims"`
	KeptAs string `json:"kept_as"` // effective value used
}

// Summary is the deterministic, reproducible aggregate.
type Summary struct {
	RunID           string          `json:"run_id"`
	Status          string          `json:"status"` // running | completed | failed | canceled
	Finalized       bool            `json:"finalized"`
	CancelRequested bool            `json:"cancel_requested"`
	Shards          []ShardSnapshot `json:"shards"`
	Tests           []TestSnapshot  `json:"tests"`
	Conflicts       []Conflict      `json:"conflicts,omitempty"`
	Counts          TestCounts      `json:"counts"`
	TotalShards     int             `json:"total_shards"`
	CrashedShards   int             `json:"crashed_shards"`
}

// TestCounts makes the headline guarantee explicit: incomplete and missing
// are reported separately from passed, never folded into success.
type TestCounts struct {
	Total      int `json:"total"`
	Passed     int `json:"passed"`
	Failed     int `json:"failed"`
	Canceled   int `json:"canceled"`
	Incomplete int `json:"incomplete"`
}

// orderedShardIDs: manifest shards first (manifest order), then extras by id.
func (m *Merger) orderedShardIDs() []string {
	planned := map[string]int{}
	order := []string{}
	for _, sd := range m.Seed {
		if _, ok := planned[sd.ShardID]; !ok {
			planned[sd.ShardID] = len(order)
			order = append(order, sd.ShardID)
		}
	}
	extra := []string{}
	for id := range m.shards {
		if _, ok := planned[id]; !ok {
			extra = append(extra, id)
		}
	}
	sort.Strings(extra)
	return append(order, extra...)
}

// orderedTestIDs: manifest tests first (first-seen order), then extras by id.
func (m *Merger) orderedTestIDs() []string {
	planned := map[string]int{}
	order := []string{}
	for _, sd := range m.Seed {
		for _, t := range sd.TestIDs {
			if _, ok := planned[t]; !ok {
				planned[t] = len(order)
				order = append(order, t)
			}
		}
	}
	extra := []string{}
	for id := range m.tests {
		if _, ok := planned[id]; !ok {
			extra = append(extra, id)
		}
	}
	sort.Strings(extra)
	return append(order, extra...)
}

// orderedAttemptIDs sorts a test's attempts by (attempt_no, attempt_id).
func orderedAttemptIDs(t *testState) []string {
	ids := make([]string, 0, len(t.attempts))
	for id := range t.attempts {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool {
		ai, aj := t.attempts[ids[i]], t.attempts[ids[j]]
		if ai.attemptNo != aj.attemptNo {
			return ai.attemptNo < aj.attemptNo
		}
		return ai.attemptID < aj.attemptID
	})
	return ids
}

// testStatus is the single source of truth for a test's effective status.
// crashedShard records shards known to have crashed.
func (m *Merger) testStatus(t *testState, crashedShard map[string]bool) string {
	if len(t.attempts) == 0 {
		// Planned test that never produced any event: missing result.
		if m.finalized {
			return StatusIncomplete
		}
		return StatusPending
	}

	latestNo := 0
	for _, a := range t.attempts {
		if a.attemptNo > latestNo {
			latestNo = a.attemptNo
		}
	}

	claims := map[string]bool{}
	hasLatestTerminal := false
	var latest []*attemptState
	for _, a := range t.attempts {
		if a.attemptNo != latestNo {
			continue
		}
		latest = append(latest, a)
		if len(a.claims) > 0 {
			hasLatestTerminal = true
			for c := range a.claims {
				claims[c] = true
			}
		}
	}

	if hasLatestTerminal {
		return outcomePrecedence(claims, attemptOrder)
	}

	// Latest attempt never terminated.
	if !m.finalized {
		return StatusRunning
	}
	// An explicit cancel wins: a test that was in flight when the run was
	// canceled is "canceled", even if its shard also crashed on the way out.
	if m.cancelRequested {
		return StatusCanceled
	}
	for _, a := range latest {
		if crashedShard[a.shardID] {
			return StatusIncomplete
		}
	}
	return StatusIncomplete
}

// Snapshot builds the aggregate from current state. The same event multiset
// always produces an identical summary (sorted keys, set-based precedence),
// regardless of arrival order — verified by the determinism tests.
func (m *Merger) Snapshot() Summary {
	sum := Summary{
		RunID:           m.RunID,
		Finalized:       m.finalized,
		CancelRequested: m.cancelRequested,
	}

	shardIDs := m.orderedShardIDs()
	for _, id := range shardIDs {
		s := m.shards[id]
		snap := ShardSnapshot{ShardID: id}
		switch {
		case len(s.statusClaims) > 0:
			snap.Status = outcomePrecedence(s.statusClaims, shardOrder)
			snap.ExitCode = s.exitCode
			snap.Error = s.errMsg
			snap.Conflict = len(s.statusClaims) > 1
		case s.finished:
			snap.Status = StatusCrashed
		case s.started:
			snap.Status = StatusRunning
		default:
			switch {
			case m.finalized:
				// Never reported after the run closed: an executor crash or
				// a dropped shard. Missing evidence is not success.
				snap.Status = StatusCrashed
			case m.cancelRequested:
				snap.Status = StatusCanceled
			default:
				snap.Status = StatusPending
			}
		}
		if len(s.warnings) > 0 {
			snap.Warnings = sortedKeys(s.warnings)
		}
		sum.Shards = append(sum.Shards, snap)
	}

	crashedShard := map[string]bool{}
	for _, sh := range sum.Shards {
		if sh.Status == StatusCrashed {
			crashedShard[sh.ShardID] = true
			sum.CrashedShards++
		}
	}

	testIDs := m.orderedTestIDs()
	for _, tid := range testIDs {
		t := m.tests[tid]
		ts := TestSnapshot{TestID: tid}

		var latestNo int
		ids := orderedAttemptIDs(t)
		for _, id := range ids {
			a := t.attempts[id]
			as := AttemptSnapshot{
				AttemptID: a.attemptID,
				AttemptNo: a.attemptNo,
				ShardID:   a.shardID,
				Started:   a.started,
			}
			if a.attemptNo > latestNo {
				latestNo = a.attemptNo
			}
			if len(a.claims) > 0 {
				for _, c := range attemptOrder {
					if a.claims[c] {
						as.Claims = append(as.Claims, c)
					}
				}
				as.FirstClaim = a.firstClaim
				as.Status = outcomePrecedence(a.claims, attemptOrder)
				as.Conflict = len(a.claims) > 1
				if as.Conflict {
					sum.Conflicts = append(sum.Conflicts, Conflict{
						Kind:   "attempt",
						ID:     a.attemptID,
						TestID: t.testID,
						Claims: joinSorted(a.claims),
						KeptAs: as.Status,
					})
				}
			} else {
				as.Status = StatusRunning
			}
			ts.Attempts = append(ts.Attempts, as)
		}
		ts.LatestAttemptNo = latestNo
		ts.Status = m.testStatus(t, crashedShard)

		sum.Tests = append(sum.Tests, ts)
		switch ts.Status {
		case StatusPassed:
			sum.Counts.Passed++
		case StatusFailed:
			sum.Counts.Failed++
		case StatusCanceled:
			sum.Counts.Canceled++
		case StatusIncomplete:
			sum.Counts.Incomplete++
			// running/pending tests are not counted until finalized
		}
		if ts.Status != StatusPending {
			sum.Counts.Total++
		}
	}

	for _, id := range shardIDs {
		s := m.shards[id]
		if len(s.statusClaims) > 1 {
			sum.Conflicts = append(sum.Conflicts, Conflict{
				Kind:   "shard",
				ID:     id,
				Claims: joinSorted(s.statusClaims),
				KeptAs: outcomePrecedence(s.statusClaims, shardOrder),
			})
		}
	}
	sort.SliceStable(sum.Conflicts, func(i, j int) bool {
		if sum.Conflicts[i].Kind != sum.Conflicts[j].Kind {
			return sum.Conflicts[i].Kind < sum.Conflicts[j].Kind
		}
		if sum.Conflicts[i].TestID != sum.Conflicts[j].TestID {
			return sum.Conflicts[i].TestID < sum.Conflicts[j].TestID
		}
		return sum.Conflicts[i].ID < sum.Conflicts[j].ID
	})

	sum.TotalShards = len(sum.Shards)
	sum.Status = m.runStatus(crashedShard, testIDs)
	return sum
}

func (m *Merger) runStatus(crashedShard map[string]bool, testIDs []string) string {
	if !m.finalized {
		if m.cancelRequested {
			return StatusCanceled
		}
		return StatusRunning
	}
	// Finalized. Honesty order: shard crashes and incomplete tests are real
	// failures; they must not be hidden by other tests passing.
	if len(crashedShard) > 0 {
		return StatusFailed
	}
	// A planned shard that never even started is also a crash.
	for _, id := range m.orderedShardIDs() {
		sh := m.shards[id]
		if len(sh.statusClaims) == 0 && !sh.started {
			return StatusFailed
		}
	}
	hasFail, hasIncomplete, hasCancel := false, false, false
	for _, tid := range testIDs {
		switch m.testStatus(m.tests[tid], crashedShard) {
		case StatusFailed:
			hasFail = true
		case StatusIncomplete:
			hasIncomplete = true
		case StatusCanceled:
			hasCancel = true
		}
	}
	switch {
	case hasFail || hasIncomplete:
		return StatusFailed
	case m.cancelRequested || hasCancel:
		return StatusCanceled
	default:
		return StatusCompleted
	}
}

func joinSorted(set map[string]bool) string {
	keys := sortedKeys(set)
	out := ""
	for i, k := range keys {
		if i > 0 {
			out += ","
		}
		out += k
	}
	return out
}
