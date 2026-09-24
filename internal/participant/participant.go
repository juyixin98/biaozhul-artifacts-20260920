// Package participant implements a 2PC participant (cohort node).
//
// A participant owns a key/value store plus a durable WAL. It answers
// prepare/commit/abort from the coordinator and NEVER decides the fate of a
// prepared transaction on its own: once PREPARE is durable the transaction is
// in the blocked ("uncertain") state until an explicit commit or abort
// arrives. There is deliberately no timeout-based abort anywhere in this
// file.
package participant

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"sync"

	"tpc/internal/crashpoint"
	"tpc/internal/wal"
)

// WAL record kinds.
const (
	KindPrepare = "PREPARE"
	KindCommit  = "COMMIT"
	KindAbort   = "ABORT"
)

// Transaction states exposed over HTTP.
const (
	StatePrepared  = "PREPARED"
	StateCommitted = "COMMITTED"
	StateAborted   = "ABORTED"
)

// Write is one key/value mutation carried by a transaction.
type Write struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

// Record is one durable WAL record of a participant.
type Record struct {
	Kind   string  `json:"kind"`
	TxID   string  `json:"txid"`
	Writes []Write `json:"writes,omitempty"` // present on PREPARE
}

// txn holds the in-memory state of one transaction touched by this node.
type txn struct {
	state  string
	writes []Write // valid while PREPARED
}

// State is a participant: WAL + reconstructed kv store, locks and txns.
type State struct {
	name string
	mu   sync.Mutex
	log  *wal.WAL

	kv   map[string]string
	txns map[string]*txn
	lock map[string]string // key -> txid holding the lock
}

// New creates a participant backed by datadir/wal.log and replays it.
func New(name, datadir string) (*State, error) {
	l, err := wal.Open(datadir)
	if err != nil {
		return nil, err
	}
	s := &State{
		name: name,
		log:  l,
		kv:   map[string]string{},
		txns: map[string]*txn{},
		lock: map[string]string{},
	}
	if err := s.replay(); err != nil {
		return nil, err
	}
	return s, nil
}

// replay rebuilds kv, txns and locks from the durable log. A PREPARE found on
// disk at boot means the node crashed (or was restarted) while blocked: the
// locks are re-acquired and the transaction stays PREPARED forever, waiting
// for the coordinator to re-drive commit/abort.
func (s *State) replay() error {
	var r Record
	return s.log.Replay(&r, func() error {
		switch r.Kind {
		case KindPrepare:
			w := append([]Write(nil), r.Writes...)
			s.txns[r.TxID] = &txn{state: StatePrepared, writes: w}
			for _, wr := range w {
				s.lock[wr.Key] = r.TxID
			}
		case KindCommit:
			if t, ok := s.txns[r.TxID]; ok {
				s.applyWrites(t.writes)
				for _, wr := range t.writes {
					delete(s.lock, wr.Key)
				}
				t.state = StateCommitted
				t.writes = nil
			} else {
				// Commit without prepare: nothing to apply (presumed abort).
				s.txns[r.TxID] = &txn{state: StateCommitted}
			}
		case KindAbort:
			if t, ok := s.txns[r.TxID]; ok {
				for _, wr := range t.writes {
					delete(s.lock, wr.Key)
				}
				t.state = StateAborted
				t.writes = nil
			} else {
				s.txns[r.TxID] = &txn{state: StateAborted}
			}
		default:
			return fmt.Errorf("participant: unknown record kind %q", r.Kind)
		}
		return nil
	})
}

// Close closes the WAL.
func (s *State) Close() error { return s.log.Close() }

// Handler returns the HTTP mux of this participant.
func (s *State) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"node": s.name, "ok": "1"})
	})
	mux.HandleFunc("POST /prepare", s.handlePrepare)
	mux.HandleFunc("POST /commit", s.handleCommit)
	mux.HandleFunc("POST /abort", s.handleAbort)
	mux.HandleFunc("POST /recover", s.handleRecover)
	mux.HandleFunc("GET /txn/{id}", s.handleTxn)
	mux.HandleFunc("GET /kv/{key}", s.handleKV)
	mux.HandleFunc("GET /state", s.handleState)
	return mux
}

type prepareReq struct {
	TxID   string  `json:"txid"`
	Writes []Write `json:"writes"`
}

// POST /prepare — phase 1. Vote no on validation failure or lock conflict;
// on success the PREPARE record is fsync'd BEFORE the yes vote is sent.
func (s *State) handlePrepare(w http.ResponseWriter, r *http.Request) {
	var req prepareReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"vote": "no", "reason": "bad json"})
		return
	}
	if req.TxID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"vote": "no", "reason": "empty txid"})
		return
	}
	for _, wr := range req.Writes {
		if wr.Key == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"vote": "no", "reason": "empty key"})
			return
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if t, ok := s.txns[req.TxID]; ok {
		// Duplicate prepare (coordinator retry): answer from current state.
		if t.state == StatePrepared {
			writeJSON(w, http.StatusOK, map[string]string{"vote": "yes"})
			return
		}
		writeJSON(w, http.StatusConflict, map[string]string{"vote": "no", "reason": "txn already " + t.state})
		return
	}

	var conflicts []string
	conflictSet := map[string]bool{}
	for _, wr := range req.Writes {
		if holder, busy := s.lock[wr.Key]; busy && holder != req.TxID && !conflictSet[wr.Key] {
			conflicts = append(conflicts, wr.Key)
			conflictSet[wr.Key] = true
		}
	}
	if len(conflicts) > 0 {
		sort.Strings(conflicts)
		// Vote NO without writing anything and without holding locks.
		writeJSON(w, http.StatusConflict, map[string]any{
			"vote":      "no",
			"reason":    "lock conflict",
			"conflicts": conflicts,
		})
		return
	}

	rec := Record{Kind: KindPrepare, TxID: req.TxID, Writes: append([]Write(nil), req.Writes...)}
	if err := s.log.Append(rec); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"vote": "no", "reason": err.Error()})
		return
	}
	// P1: crash after PREPARE is durable but before the yes vote is sent.
	crashpoint.Hit("P1")

	s.txns[req.TxID] = &txn{state: StatePrepared, writes: rec.Writes}
	for _, wr := range req.Writes {
		s.lock[wr.Key] = req.TxID
	}
	writeJSON(w, http.StatusOK, map[string]string{"vote": "yes"})
}

type decisionReq struct {
	TxID string `json:"txid"`
}

// POST /commit — phase 2 commit. Idempotent: repeated commits ack; an unknown
// transaction acks as presumed-abort so a coordinator can finish its retry
// loop (it must then treat this as "nothing committed here").
func (s *State) handleCommit(w http.ResponseWriter, r *http.Request) {
	var req decisionReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TxID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad request"})
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	t, ok := s.txns[req.TxID]
	switch {
	case !ok:
		writeJSON(w, http.StatusOK, map[string]string{"status": StateAborted, "note": "unknown txn, presumed abort"})
		return
	case t.state == StateCommitted:
		writeJSON(w, http.StatusOK, map[string]string{"status": StateCommitted})
		return
	case t.state == StateAborted:
		// Commit asked for an already-aborted txn: a real disagreement the
		// coordinator must see (cannot happen under a correct coordinator).
		writeJSON(w, http.StatusConflict, map[string]string{"status": StateAborted, "error": "txn already aborted"})
		return
	}

	if err := s.log.Append(Record{Kind: KindCommit, TxID: req.TxID}); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	// P2: crash after COMMIT is durable but before applying and acking.
	crashpoint.Hit("P2")

	s.applyWrites(t.writes)
	for _, wr := range t.writes {
		delete(s.lock, wr.Key)
	}
	t.state = StateCommitted
	t.writes = nil
	writeJSON(w, http.StatusOK, map[string]string{"status": StateCommitted})
}

// POST /abort — phase 2 abort. Idempotent; unknown txns ack (presumed abort).
func (s *State) handleAbort(w http.ResponseWriter, r *http.Request) {
	var req decisionReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TxID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad request"})
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	t, ok := s.txns[req.TxID]
	switch {
	case !ok:
		writeJSON(w, http.StatusOK, map[string]string{"status": StateAborted, "note": "unknown txn, presumed abort"})
		return
	case t.state == StateAborted:
		// Idempotent re-delivery of the same terminal decision: ack.
		writeJSON(w, http.StatusOK, map[string]string{"status": StateAborted})
		return
	case t.state == StateCommitted:
		writeJSON(w, http.StatusConflict, map[string]string{"status": StateCommitted, "error": "txn already committed"})
		return
	}

	if err := s.log.Append(Record{Kind: KindAbort, TxID: req.TxID}); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	// P3: crash after ABORT is durable but before releasing locks/acking.
	crashpoint.Hit("P3")

	for _, wr := range t.writes {
		delete(s.lock, wr.Key)
	}
	t.state = StateAborted
	t.writes = nil
	writeJSON(w, http.StatusOK, map[string]string{"status": StateAborted})
}

// BlockedTxn is one prepared transaction still waiting for a decision.
type BlockedTxn struct {
	TxID string   `json:"txid"`
	Keys []string `json:"keys"`
}

// POST /recover — a participant cannot resolve a prepared transaction by
// itself; recovery only REPORTS which txns are blocked. The coordinator (or
// an operator) must re-drive them. This is the explicit blocking guarantee.
func (s *State) handleRecover(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	blocked := s.blockedLocked()
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"node":    s.name,
		"blocked": blocked,
		"note":    "prepared txns are never aborted on timeout; they wait for the coordinator",
	})
}

func (s *State) handleTxn(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.txns[id]
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"txid": id, "status": "UNKNOWN"})
		return
	}
	keys := make([]string, 0)
	for _, wr := range t.writes {
		keys = append(keys, wr.Key)
	}
	sort.Strings(keys)
	writeJSON(w, http.StatusOK, map[string]any{
		"txid":    id,
		"status":  t.state,
		"blocked": t.state == StatePrepared,
		"keys":    keys,
	})
}

func (s *State) handleKV(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	s.mu.Lock()
	v, lockedBy := s.kv[key], s.lock[key]
	s.mu.Unlock()
	resp := map[string]any{"key": key, "value": v, "locked": lockedBy != ""}
	if lockedBy != "" {
		resp["locked_by"] = lockedBy
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *State) handleState(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	txns := map[string]string{}
	for id, t := range s.txns {
		txns[id] = t.state
	}
	locks := make([]string, 0, len(s.lock))
	for k := range s.lock {
		locks = append(locks, k)
	}
	sort.Strings(locks)
	writeJSON(w, http.StatusOK, map[string]any{
		"node":  s.name,
		"txns":  txns,
		"locks": locks,
		"kv":    s.kv,
	})
}

// Blocked returns the prepared transactions currently awaiting a decision.
func (s *State) Blocked() []BlockedTxn {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.blockedLocked()
}

func (s *State) blockedLocked() []BlockedTxn {
	out := make([]BlockedTxn, 0)
	for id, t := range s.txns {
		if t.state != StatePrepared {
			continue
		}
		keys := make([]string, 0, len(t.writes))
		for _, wr := range t.writes {
			keys = append(keys, wr.Key)
		}
		sort.Strings(keys)
		out = append(out, BlockedTxn{TxID: id, Keys: keys})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TxID < out[j].TxID })
	return out
}

// applyWrites mutates the kv store. Keys are applied in sorted order so a
// replay is byte-for-byte deterministic even with unsorted request writes.
func (s *State) applyWrites(ws []Write) {
	cp := append([]Write(nil), ws...)
	sort.Slice(cp, func(i, j int) bool { return cp[i].Key < cp[j].Key })
	for _, wr := range cp {
		s.kv[wr.Key] = wr.Value
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
