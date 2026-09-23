// Package harness wires a JSON scenario into the deterministic engine:
// it creates the coordinator and three participants, installs crash rules
// (by tick or by protocol milestone), runs the simulation and then judges
// outcomes and safety invariants from the durable WAL files on disk.
package harness

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"twopc-sim/internal/engine"
	"twopc-sim/internal/twopc"
	"twopc-sim/internal/wal"
)

// Request is one client transaction.
type Request struct {
	TxnID  string        `json:"txnId"`
	At     int64         `json:"at"`
	Writes []wal.KVWrite `json:"writes"`
}

// Crash describes one crash injection. Exactly one of At / Milestone must be
// set: At crashes at a virtual tick; Milestone crashes when the named node
// first reaches that protocol milestone.
type Crash struct {
	Node      string `json:"node"`
	At        *int64 `json:"at,omitempty"`
	Milestone string `json:"milestone,omitempty"`
	DownTicks int64  `json:"downTicks"` // 0 = stays down for the whole run
	Repeat    int    `json:"repeat"`    // 0 = fires once; N = first N matches
}

// Spec is the JSON scenario document.
type Spec struct {
	Seed      int64                        `json:"seed"`
	Horizon   int64                        `json:"horizon"`
	Network   engine.NetConfig             `json:"network"`
	DataDir   string                       `json:"dataDir,omitempty"`
	Requests  []Request                    `json:"requests"`
	Crashes   []Crash                      `json:"crashes"`
	InitialKV map[string]map[string]string `json:"initialKv,omitempty"`
}

// BlockedInfo documents one transaction that is blocked at run end.
type BlockedInfo struct {
	TxnID         string   `json:"txnId"`
	PreparedNodes []string `json:"preparedNodes"`
	CoordinatorUp bool     `json:"coordinatorUp"`
	Reason        string   `json:"reason"`
	QueryCount    int      `json:"queryCount"`
}

// TxnOutcome is the judged result for one client transaction.
type TxnOutcome struct {
	TxnID          string   `json:"txnId"`
	Status         string   `json:"status"` // committed | aborted | blocked | ongoing
	CommittedNodes []string `json:"committedNodes,omitempty"`
	AbortedNodes   []string `json:"abortedNodes,omitempty"`
	PreparedNodes  []string `json:"preparedNodes,omitempty"`
	CoordinatorUp  bool     `json:"coordinatorUp"`
	ClientReported string   `json:"clientReported,omitempty"`
}

// Result is the full run output.
type Result struct {
	Spec            Spec                         `json:"spec"`
	Trace           []engine.Record              `json:"trace"`
	Transactions    []TxnOutcome                 `json:"transactions"`
	Blocked         []BlockedInfo                `json:"blocked"`
	FinalKV         map[string]map[string]string `json:"finalKv"`
	InvariantErrors []string                     `json:"invariantErrors"`
	DataDir         string                       `json:"dataDir"`
}

// LoadSpec parses a scenario file.
func LoadSpec(path string) (Spec, error) {
	var s Spec
	data, err := os.ReadFile(path)
	if err != nil {
		return s, err
	}
	if err := json.Unmarshal(data, &s); err != nil {
		return s, err
	}
	if s.Horizon == 0 {
		return s, fmt.Errorf("scenario %s: horizon is required", path)
	}
	return s, nil
}

const coordID engine.NodeID = "coord"

var participantIDs = []engine.NodeID{"p1", "p2", "p3"}

// Run executes a scenario and returns the judged result.
func Run(s Spec) (Result, error) {
	dataDir := s.DataDir
	cleanup := false
	if dataDir == "" {
		var err error
		dataDir, err = os.MkdirTemp("", "twopc-")
		if err != nil {
			return Result{}, err
		}
		cleanup = true
	} else {
		if err := os.MkdirAll(dataDir, 0o755); err != nil {
			return Result{}, err
		}
	}
	defer func() {
		if cleanup {
			_ = os.RemoveAll(dataDir)
		}
	}()

	res := Result{Spec: s, DataDir: dataDir}
	var trace []engine.Record
	eng := engine.New(s.Seed, func(r engine.Record) { trace = append(trace, r) }, s.Network)

	coord, err := twopc.NewCoordinator(eng, coordID, participantIDs, dataDir)
	if err != nil {
		return res, err
	}
	var parts []*twopc.Participant
	for _, pid := range participantIDs {
		p, err := twopc.NewParticipant(eng, pid, coordID, dataDir, s.InitialKV[string(pid)])
		if err != nil {
			return res, err
		}
		parts = append(parts, p)
	}
	eng.Register(coord)
	for _, p := range parts {
		eng.Register(p)
	}

	// --- crash rules ---------------------------------------------------
	tickCrash := map[int64][]Crash{}
	var mileCrash []Crash
	remaining := map[int]int{} // index -> remaining matches
	for i, c := range s.Crashes {
		repeat := c.Repeat
		if repeat == 0 {
			repeat = 1
		}
		remaining[i] = repeat
		if c.Milestone != "" {
			mileCrash = append(mileCrash, c)
		} else if c.At != nil {
			tickCrash[*c.At] = append(tickCrash[*c.At], c)
		}
	}

	hook := engine.CrashHook(func(id engine.NodeID, milestone string) bool {
		crashedSelf := false
		for i, c := range mileCrash {
			if remaining[i] <= 0 {
				continue
			}
			if engine.NodeID(c.Node) != id || c.Milestone != milestone {
				continue
			}
			remaining[i]--
			eng.Crash(id, c.DownTicks)
			if engine.NodeID(c.Node) == id {
				crashedSelf = true
			}
		}
		return crashedSelf
	})
	coord.Hook = hook
	for _, p := range parts {
		p.Hook = hook
	}

	// tick-absolute crashes fire before any normal event at that tick
	for at, cs := range tickCrash {
		cs := cs
		at := at
		eng.ScheduleFunc(at, func(now int64) {
			for _, c := range cs {
				eng.Crash(engine.NodeID(c.Node), c.DownTicks)
			}
		})
	}

	// --- client requests -----------------------------------------------
	for _, req := range s.Requests {
		req := req
		at := req.At
		eng.ScheduleFunc(at, func(now int64) {
			coord.Begin(req.TxnID, req.Writes)
		})
	}

	eng.Run(s.Horizon)
	res.Trace = trace

	judge(&res, coord, parts)
	return res, nil
}

type durableState struct {
	hasBegin  bool
	decision  string // "" | commit | abort
	prepared  map[string]bool
	committed map[string]bool
	aborted   map[string]bool
}

func newDurableState() *durableState {
	return &durableState{
		prepared:  map[string]bool{},
		committed: map[string]bool{},
		aborted:   map[string]bool{},
	}
}

func judge(res *Result, coord *twopc.Coordinator, parts []*twopc.Participant) {
	coordRecs, _ := wal.ReadAll(res.DataDir, string(coordID))
	cs := newDurableState()
	for _, r := range coordRecs {
		switch r.Kind {
		case "begin":
			cs.hasBegin = true
		case "commit":
			cs.decision = "commit"
		case "abort":
			cs.decision = "abort"
		}
	}

	partState := map[string]*durableState{}
	for _, p := range parts {
		ps := newDurableState()
		recs, _ := wal.ReadAll(res.DataDir, string(p.ID()))
		for _, r := range recs {
			switch r.Kind {
			case "prepared":
				ps.prepared[r.TxnID] = true
			case "commit":
				ps.committed[r.TxnID] = true
				delete(ps.prepared, r.TxnID)
			case "abort":
				ps.aborted[r.TxnID] = true
				delete(ps.prepared, r.TxnID)
			}
		}
		partState[string(p.ID())] = ps
	}

	coordUp := !coord.IsDown()
	for _, req := range res.Spec.Requests {
		txn := req.TxnID
		out := TxnOutcome{TxnID: txn, CoordinatorUp: coordUp}
		commits, aborts, prepared := []string{}, []string{}, []string{}
		for _, pid := range []string{"p1", "p2", "p3"} {
			st := partState[pid]
			if st.committed[txn] {
				commits = append(commits, pid)
			} else if st.aborted[txn] {
				aborts = append(aborts, pid)
			} else if st.prepared[txn] {
				prepared = append(prepared, pid)
			}
		}
		out.CommittedNodes, out.AbortedNodes, out.PreparedNodes = commits, aborts, prepared
		if v, ok := coord.ClientOutcome(txn); ok {
			out.ClientReported = v
		}

		switch {
		case len(commits) > 0 && len(aborts) > 0:
			// Divergent durable decisions = genuine atomicity violation.
			out.Status = "partial-commit!"
			res.InvariantErrors = append(res.InvariantErrors,
				fmt.Sprintf("txn %s: PARTIAL COMMIT: committed=%v aborted=%v prepared=%v",
					txn, commits, aborts, prepared))
		case len(commits) == 3:
			out.Status = "committed"
		case len(aborts) == 3:
			out.Status = "aborted"
		case len(commits) > 0:
			// Some participants durable-committed while others are still
			// prepared: the global decision is COMMIT and cannot change, but
			// termination is blocked by unreachable node(s). This is legal
			// blocking 2PC, NOT a partial commit.
			out.Status = "blocked"
			res.Blocked = append(res.Blocked, blockedInfo(txn, prepared, coordUp, res.Trace,
				fmt.Sprintf("COMMIT is global: %v already durable-committed, %v still hold PREPARED and cannot finish",
					commits, prepared)))
		case len(aborts) > 0:
			// Some durable-aborted, others still prepared with no decision
			// delivered: blocked on the decision propagation.
			out.Status = "blocked"
			res.Blocked = append(res.Blocked, blockedInfo(txn, prepared, coordUp, res.Trace,
				fmt.Sprintf("ABORT is global: %v durable-aborted, %v still hold PREPARED", aborts, prepared)))
		case len(prepared) > 0:
			out.Status = "blocked"
			reason := "participants hold durable PREPARED and have not received the decision"
			if !coordUp {
				reason = "coordinator is DOWN; prepared participants cannot learn the decision and must not abort unilaterally"
			}
			res.Blocked = append(res.Blocked, blockedInfo(txn, prepared, coordUp, res.Trace, reason))
		default:
			// Nobody reached prepared.
			if coordUp {
				out.Status = "ongoing"
			} else {
				out.Status = "blocked"
				res.Blocked = append(res.Blocked, BlockedInfo{
					TxnID: txn, CoordinatorUp: false,
					Reason: "coordinator DOWN before phase-1 completed; outcome unresolved",
				})
			}
		}
		res.Transactions = append(res.Transactions, out)
	}

	// --- global safety invariants --------------------------------------
	// 1. No node durably records both commit and abort for a txn.
	checkNoConflictingDecisions(res, partState)
	// 2. Coordinator decision is durable before any decision message is sent.
	checkDecisionDurableBeforeSend(res)

	// final KV: committed application state of each participant
	res.FinalKV = map[string]map[string]string{}
	for _, p := range parts {
		if p.IsDown() {
			res.FinalKV[string(p.ID())] = map[string]string{"__state": "CRASHED-DOWN (durable WAL on disk, volatile state unavailable)"}
			continue
		}
		kv := map[string]string{}
		for k, v := range p.KV() {
			kv[k] = v
		}
		res.FinalKV[string(p.ID())] = kv
	}
}

func checkNoConflictingDecisions(res *Result, partState map[string]*durableState) {
	for pid, st := range partState {
		for txn := range st.committed {
			if st.aborted[txn] {
				res.InvariantErrors = append(res.InvariantErrors,
					fmt.Sprintf("participant %s durable commit AND abort for txn %s", pid, txn))
			}
		}
	}
	recs, _ := wal.ReadAll(res.DataDir, string(coordID))
	seen := map[string]string{}
	for _, r := range recs {
		if r.Kind != "commit" && r.Kind != "abort" {
			continue
		}
		if prev, ok := seen[r.TxnID]; ok && prev != r.Kind {
			res.InvariantErrors = append(res.InvariantErrors,
				fmt.Sprintf("coordinator durable %s then %s for txn %s", prev, r.Kind, r.TxnID))
		}
		seen[r.TxnID] = r.Kind
	}
}

// checkDecisionDurableBeforeSend verifies the write-ahead rule: the first
// durable coordinator decision precedes the first decision broadcast.
func checkDecisionDurableBeforeSend(res *Result) {
	firstDurable := map[string]int{}
	firstSend := map[string]int{}
	for i, r := range res.Trace {
		if r.Kind == "durable" && r.Node == string(coordID) &&
			(strings.Contains(r.Detail, "record=commit") || strings.Contains(r.Detail, "record=abort")) {
			txn := extractField(r.Detail, "txn")
			if _, ok := firstDurable[txn]; !ok {
				firstDurable[txn] = i
			}
		}
		if r.Kind == "decision-send" && r.Node == string(coordID) {
			txn := extractField(r.Detail, "txn")
			if _, ok := firstSend[txn]; !ok {
				firstSend[txn] = i
			}
		}
	}
	for txn, si := range firstSend {
		di, ok := firstDurable[txn]
		if !ok {
			res.InvariantErrors = append(res.InvariantErrors,
				fmt.Sprintf("txn %s: decision broadcast with NO durable decision record", txn))
			continue
		}
		if di > si {
			res.InvariantErrors = append(res.InvariantErrors,
				fmt.Sprintf("txn %s: decision sent (trace#%d) before durable record (trace#%d)", txn, si, di))
		}
	}
}

func blockedInfo(txn string, prepared []string, coordUp bool, trace []engine.Record, reason string) BlockedInfo {
	return BlockedInfo{
		TxnID:         txn,
		PreparedNodes: prepared,
		CoordinatorUp: coordUp,
		Reason:        reason,
		QueryCount:    countDetail(trace, "send", "type=twopc.Query"),
	}
}

func extractField(detail, field string) string {
	for _, p := range strings.Split(detail, " ") {
		if strings.HasPrefix(p, field+"=") {
			return strings.TrimPrefix(p, field+"=")
		}
	}
	return ""
}

func countDetail(trace []engine.Record, kind, contains string) int {
	n := 0
	for _, r := range trace {
		if r.Kind == kind && strings.Contains(r.Detail, contains) {
			n++
		}
	}
	return n
}
