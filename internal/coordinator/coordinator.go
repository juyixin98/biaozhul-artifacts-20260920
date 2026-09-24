// Package coordinator implements the 2PC coordinator.
//
// Durable protocol:
//
//  1. BEGIN record (carrying the full, per-participant write set) is fsync'd.
//  2. Phase 1: prepare is sent to every participant. Any vote "no", bad
//     response or unreachable participant makes the decision ABORT.
//  3. The decision (COMMIT only if every participant voted yes, else ABORT)
//     is fsync'd BEFORE any phase-2 message leaves the coordinator. This is
//     the atomic-commit boundary: once COMMIT is durable, no recovery path
//     may ever abort that transaction.
//  4. Phase 2: the decision is delivered with bounded retries. Once every
//     participant acks, a DONE record is fsync'd.
//
// Restart recovery:
//   - BEGIN with no decision -> ABORT (no commit record exists anywhere, so
//     abort is always safe); phase-2 abort is delivered to prepared
//     participants that had voted yes before the crash. They block until it
//     arrives and must not abort themselves.
//   - decided but not DONE -> the same decision keeps being re-delivered.
//   - DONE -> forgotten.
package coordinator

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"sort"
	"sync"
	"time"

	"tpc/internal/crashpoint"
	"tpc/internal/participant"
	"tpc/internal/wal"
)

const (
	KindBegin  = "BEGIN"
	KindCommit = "COMMIT"
	KindAbort  = "ABORT"
	KindDone   = "DONE"

	decisionCommit = "COMMIT"
	decisionAbort  = "ABORT"

	// httpTimeout bounds a single phase-1/phase-2 RPC.
	httpTimeout = 2 * time.Second
	// syncDeliver bounds how long the submit request waits for phase-2 acks.
	// Afterwards the client gets COMMIT_UNCERTAIN; the decision is already
	// durable and the background recovery loop keeps retrying.
	syncDeliver = 3 * time.Second
)

// ShardWrite is one mutation tagged with the participant that owns the key.
type ShardWrite struct {
	Participant string `json:"participant"`
	Key         string `json:"key"`
	Value       string `json:"value"`
}

// Record is one durable WAL record of the coordinator.
type Record struct {
	Kind   string       `json:"kind"`
	TxID   string       `json:"txid"`
	Writes []ShardWrite `json:"writes,omitempty"` // BEGIN only
}

type partTarget struct {
	name   string
	url    string
	writes []participant.Write
}

type txn struct {
	id       string
	decision string // "" while undecided
	writes   []ShardWrite
	targets  []*partTarget
	done     bool
	acked    map[string]bool
}

// State is the coordinator. parts maps participant name -> base URL.
type State struct {
	name   string
	parts  map[string]string
	mu     sync.Mutex
	log    *wal.WAL
	txns   map[string]*txn
	client *http.Client
	logger *log.Logger
}

// New creates a coordinator backed by datadir/wal.log, replays it and keeps
// the participant address table used both for new transactions and for
// rebuilding fan-out targets after restart.
func New(name, datadir string, parts map[string]string, logger *log.Logger) (*State, error) {
	l, err := wal.Open(datadir)
	if err != nil {
		return nil, err
	}
	if logger == nil {
		logger = log.Default()
	}
	s := &State{
		name:   name,
		parts:  parts,
		log:    l,
		txns:   map[string]*txn{},
		client: &http.Client{Timeout: httpTimeout},
		logger: logger,
	}
	if err := s.replay(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *State) replay() error {
	var r Record
	return s.log.Replay(&r, func() error {
		switch r.Kind {
		case KindBegin:
			if _, exists := s.txns[r.TxID]; exists {
				return nil
			}
			targets, err := s.buildTargets(r.Writes)
			if err != nil {
				return err
			}
			s.txns[r.TxID] = &txn{
				id:      r.TxID,
				writes:  append([]ShardWrite(nil), r.Writes...),
				targets: targets,
				acked:   map[string]bool{},
			}
		case KindCommit, KindAbort:
			t, ok := s.txns[r.TxID]
			if !ok {
				return fmt.Errorf("coordinator: decision without BEGIN for %s", r.TxID)
			}
			t.decision = r.Kind
		case KindDone:
			if t, ok := s.txns[r.TxID]; ok {
				t.done = true
			}
		default:
			return fmt.Errorf("coordinator: unknown record kind %q", r.Kind)
		}
		return nil
	})
}

// Close closes the WAL.
func (s *State) Close() error { return s.log.Close() }

// RunRecovery performs one recovery sweep:
//   - decided, not complete transactions re-deliver their durable decision;
//   - BEGIN-only transactions are aborted (safe: no COMMIT can exist).
//
// It returns a txid -> outcome summary.
func (s *State) RunRecovery(ctx context.Context) map[string]string {
	s.mu.Lock()
	list := make([]*txn, 0, len(s.txns))
	for _, t := range s.txns {
		list = append(list, t)
	}
	s.mu.Unlock()
	sort.Slice(list, func(i, j int) bool { return list[i].id < list[j].id })

	out := map[string]string{}
	for _, t := range list {
		s.mu.Lock()
		decided := t.decision != ""
		done := t.done
		s.mu.Unlock()

		switch {
		case done:
			out[t.id] = t.decision + "_COMPLETE"
		case decided:
			s.deliver(ctx, t)
			s.mu.Lock()
			if t.done {
				out[t.id] = t.decision + "_COMPLETE"
			} else {
				out[t.id] = t.decision + "_PENDING"
			}
			s.mu.Unlock()
		default:
			s.forceAbort(t)
			s.deliver(ctx, t)
			out[t.id] = "ABORTED_ON_RECOVERY"
		}
	}
	return out
}

// forceAbort fsyncs an ABORT decision for a BEGIN-only transaction.
func (s *State) forceAbort(t *txn) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if t.decision != "" {
		return
	}
	if err := s.log.Append(Record{Kind: KindAbort, TxID: t.id}); err != nil {
		s.logger.Printf("coordinator: cannot log abort for %s: %v", t.id, err)
		return
	}
	t.decision = decisionAbort
}

// deliver resends the durable decision to every not-yet-acked target and
// logs DONE once all acks are in. A participant answering for an unknown
// transaction (presumed abort) acks, so retry loops always terminate.
// Returns true when the transaction is fully acknowledged.
func (s *State) deliver(ctx context.Context, t *txn) bool {
	s.mu.Lock()
	decision := t.decision
	targets := append([]*partTarget(nil), t.targets...)
	s.mu.Unlock()
	if decision == "" {
		return false
	}
	path := "/commit"
	if decision == decisionAbort {
		path = "/abort"
	}

	for _, tg := range targets {
		for attempt := 0; attempt < 5; attempt++ {
			s.mu.Lock()
			acked := t.acked[tg.name]
			s.mu.Unlock()
			if acked {
				break
			}
			code, data, err := s.postJSON(ctx, tg.url+path, map[string]string{"txid": t.id})
			if err == nil && deliveryDone(decision, code, data) {
				s.mu.Lock()
				t.acked[tg.name] = true
				s.mu.Unlock()
				break
			}
			if err != nil {
				s.logger.Printf("coordinator: %s -> %s attempt %d: %v", path, tg.name, attempt, err)
			}
			if ctx.Err() != nil {
				break
			}
			select {
			case <-ctx.Done():
			case <-time.After(time.Duration(50*(attempt+1)) * time.Millisecond):
			}
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if len(t.acked) == len(t.targets) && !t.done {
		if err := s.log.Append(Record{Kind: KindDone, TxID: t.id}); err != nil {
			s.logger.Printf("coordinator: log DONE %s: %v", t.id, err)
			return false
		}
		// C3: crash after DONE is durable, before the client can be told.
		crashpoint.Hit("C3")
		t.done = true
	}
	return t.done
}

// StartRecoveryLoop periodically re-drives unresolved transactions until
// shutdown. This is what eventually unblocks prepared participants after a
// coordinator restart, without them ever timing out on their own.
func (s *State) StartRecoveryLoop(ctx context.Context, interval time.Duration) {
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				func() {
					ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
					defer cancel()
					s.RunRecovery(ctx)
				}()
			}
		}
	}()
}

// Handler returns the coordinator HTTP mux.
func (s *State) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"node": s.name, "ok": "1"})
	})
	mux.HandleFunc("POST /txn", s.handleSubmit)
	mux.HandleFunc("POST /recover", s.handleRecover)
	mux.HandleFunc("GET /txn/{id}", s.handleTxn)
	return mux
}

type submitReq struct {
	TxID   string       `json:"txid"`
	Writes []ShardWrite `json:"writes"`
}

// POST /txn runs the full two-phase commit for one transaction.
func (s *State) handleSubmit(w http.ResponseWriter, r *http.Request) {
	var req submitReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad json"})
		return
	}
	if req.TxID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "empty txid"})
		return
	}
	targets, err := s.buildTargets(req.Writes)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	s.mu.Lock()
	if existing, dup := s.txns[req.TxID]; dup {
		status := "IN_PROGRESS"
		switch {
		case existing.done:
			status = existing.decision + "_COMPLETE"
		case existing.decision != "":
			status = existing.decision + "_PENDING"
		}
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, map[string]any{"txid": req.TxID, "status": status})
		return
	}
	if err := s.log.Append(Record{Kind: KindBegin, TxID: req.TxID, Writes: req.Writes}); err != nil {
		s.mu.Unlock()
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	// C1: crash right after BEGIN is durable, before any prepare is sent.
	crashpoint.Hit("C1")
	t := &txn{id: req.TxID, writes: req.Writes, targets: targets, acked: map[string]bool{}}
	s.txns[req.TxID] = t
	s.mu.Unlock()

	pctx, pcancel := context.WithTimeout(r.Context(), 30*time.Second)
	votes := s.phase1(pctx, t)
	pcancel()

	allYes := true
	for _, tg := range targets {
		if votes[tg.name] != "yes" {
			allYes = false
		}
	}
	decision := decisionAbort
	if allYes {
		decision = decisionCommit
	}

	s.mu.Lock()
	if err := s.log.Append(Record{Kind: decision, TxID: t.id}); err != nil {
		s.mu.Unlock()
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	// C2: crash after the decision is durable but before phase 2 is sent.
	crashpoint.Hit("C2")
	t.decision = decision
	s.mu.Unlock()

	dctx, dcancel := context.WithTimeout(r.Context(), syncDeliver)
	complete := s.deliver(dctx, t)
	dcancel()

	resp := map[string]any{"txid": t.id, "decision": decision}
	status := "COMMITTED"
	code := http.StatusOK
	switch {
	case complete && decision == decisionAbort:
		status = "ABORTED"
	case !complete && decision == decisionCommit:
		status = "COMMIT_UNCERTAIN"
		code = http.StatusBadGateway
	case !complete:
		status = "ABORT_PENDING"
		code = http.StatusAccepted
	}
	resp["status"] = status
	resp["votes"] = votes
	writeJSON(w, code, resp)
}

// phase1 sends prepare to every target concurrently and maps name to either
// "yes" or a failure reason. An unreachable participant is a "no": the txn
// aborts and any participant that did prepare gets the phase-2 abort;
// nothing can commit anywhere.
func (s *State) phase1(ctx context.Context, t *txn) map[string]string {
	var mu sync.Mutex
	votes := map[string]string{}
	var wg sync.WaitGroup
	for _, tg := range t.targets {
		wg.Add(1)
		go func(tg *partTarget) {
			defer wg.Done()
			ws := make([]participant.Write, 0, len(tg.writes))
			ws = append(ws, tg.writes...)
			code, _, err := s.postJSON(ctx, tg.url+"/prepare", map[string]any{
				"txid":   t.id,
				"writes": ws,
			})
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err != nil:
				votes[tg.name] = "unreachable"
			case code == http.StatusOK:
				votes[tg.name] = "yes"
			default:
				votes[tg.name] = fmt.Sprintf("vote_no:%d", code)
			}
		}(tg)
	}
	wg.Wait()
	return votes
}

func (s *State) handleRecover(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"recovered": s.RunRecovery(r.Context())})
}

// GET /txn/{id}: UNKNOWN / UNDECIDED / *_PENDING / *_COMPLETE.
func (s *State) handleTxn(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.mu.Lock()
	t, ok := s.txns[id]
	if !ok {
		s.mu.Unlock()
		writeJSON(w, http.StatusNotFound, map[string]string{"txid": id, "status": "UNKNOWN"})
		return
	}
	resp := map[string]any{"txid": id}
	switch {
	case t.done:
		resp["status"] = t.decision + "_COMPLETE"
	case t.decision != "":
		resp["status"] = t.decision + "_PENDING"
	default:
		resp["status"] = "UNDECIDED"
	}
	s.mu.Unlock()
	code := http.StatusOK
	if resp["status"] == "UNDECIDED" {
		code = http.StatusConflict
	}
	writeJSON(w, code, resp)
}

func (s *State) postJSON(ctx context.Context, url string, body any) (int, []byte, error) {
	buf, err := json.Marshal(body)
	if err != nil {
		return 0, nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(buf))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 500 {
		return resp.StatusCode, data, errors.New("http " + resp.Status)
	}
	return resp.StatusCode, data, nil
}

// buildTargets shards writes by participant and validates names against the
// configured cluster.
func (s *State) buildTargets(ws []ShardWrite) ([]*partTarget, error) {
	if len(ws) == 0 {
		return nil, fmt.Errorf("no writes")
	}
	byName := map[string][]participant.Write{}
	for _, wr := range ws {
		if wr.Participant == "" || wr.Key == "" {
			return nil, fmt.Errorf("each write needs participant and key")
		}
		if _, ok := s.parts[wr.Participant]; !ok {
			return nil, fmt.Errorf("unknown participant %q", wr.Participant)
		}
		byName[wr.Participant] = append(byName[wr.Participant], participant.Write{Key: wr.Key, Value: wr.Value})
	}
	names := make([]string, 0, len(byName))
	for n := range byName {
		names = append(names, n)
	}
	sort.Strings(names)
	out := make([]*partTarget, 0, len(names))
	for _, n := range names {
		out = append(out, &partTarget{name: n, url: s.parts[n], writes: byName[n]})
	}
	return out, nil
}

// deliveryDone decides whether a participant response means the durable
// decision has been acknowledged:
//   - 200: applied / idempotent same-state ack;
//   - 404: presumed abort (the participant never prepared anything);
//   - 409 with a status field matching the decision being delivered:
//     idempotent observation of the same terminal state.
func deliveryDone(decision string, code int, body []byte) bool {
	if code == http.StatusOK || code == http.StatusNotFound {
		return true
	}
	if code != http.StatusConflict {
		return false
	}
	var r struct {
		Status string `json:"status"`
	}
	if err := json.Unmarshal(body, &r); err != nil || r.Status == "" {
		return false
	}
	return r.Status == decision
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
