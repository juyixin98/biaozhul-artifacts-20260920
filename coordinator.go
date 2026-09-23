package tpc

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// Tx is the coordinator's in-memory view of one transaction. The WAL is
// authoritative; this is rebuilt from it at startup.
type Tx struct {
	ID           string
	Participants []string
	Payload      json.RawMessage
	State        string // PREPARING, COMMIT, ABORT, DONE
	Decision     string // COMMIT or ABORT once decided
}

// Coordinator drives the two-phase commit protocol. Its only persistent
// state is the WAL; after a crash it reloads the log and finishes whatever
// was in flight:
//
//   - PREPARING (no durable decision)  -> decide ABORT and deliver it
//   - COMMIT / ABORT (decision durable) -> re-deliver the decision until
//     every participant has acked, then log DONE
//   - DONE                              -> nothing to do
type Coordinator struct {
	wal           *WAL
	mu            sync.Mutex
	txs           map[string]*Tx
	client        *http.Client
	retryInterval time.Duration
	stopCh        chan struct{}
}

// NewCoordinator opens (or recovers) a coordinator whose durable log lives
// at walPath. retryInterval <= 0 selects a default.
func NewCoordinator(walPath string, retryInterval time.Duration) (*Coordinator, error) {
	if retryInterval <= 0 {
		retryInterval = 200 * time.Millisecond
	}
	wal, recs, err := OpenWAL(walPath)
	if err != nil {
		return nil, err
	}
	c := &Coordinator{
		wal:           wal,
		txs:           map[string]*Tx{},
		client:        &http.Client{Timeout: 2 * time.Second},
		retryInterval: retryInterval,
		stopCh:        make(chan struct{}),
	}
	for _, r := range recs {
		tx := c.txs[r.TxID]
		if tx == nil {
			tx = &Tx{ID: r.TxID}
			c.txs[r.TxID] = tx
		}
		tx.State = r.State
		if r.Participants != nil {
			tx.Participants = r.Participants
		}
		if r.Payload != nil {
			tx.Payload = r.Payload
		}
		if r.Decision != "" {
			tx.Decision = r.Decision
		}
	}
	// Recovery: finish every transaction that was not DONE when we crashed.
	for _, tx := range c.txs {
		switch tx.State {
		case StatePreparing:
			// Crashed before the decision was durable. The only safe
			// decision is ABORT: some participants may never have been
			// prepared, and none may have committed.
			tx.State = StateAbort
			tx.Decision = StateAbort
			if err := c.wal.Append(Record{TxID: tx.ID, State: StateAbort}); err != nil {
				return nil, err
			}
			go c.driveDecision(tx)
		case StateCommit, StateAbort:
			tx.Decision = tx.State
			go c.driveDecision(tx)
		case StateDone:
			// fully delivered before the crash
		}
	}
	return c, nil
}

// Handler returns the coordinator's HTTP API:
//
//	POST /tx              {"id","participants":[urls],"payload":{...}} -> 201
//	POST /tx/{id}/commit  run the full 2PC protocol to a decision
//	POST /tx/{id}/abort   abort before a commit decision
//	GET  /tx/{id}         current state (used by recovering participants)
func (c *Coordinator) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /tx", c.handleCreate)
	mux.HandleFunc("POST /tx/{id}/commit", c.handleCommit)
	mux.HandleFunc("POST /tx/{id}/abort", c.handleAbort)
	mux.HandleFunc("GET /tx/{id}", c.handleGet)
	return mux
}

// Close stops background recovery loops and closes the log.
func (c *Coordinator) Close() error {
	close(c.stopCh)
	return c.wal.Close()
}

func (c *Coordinator) handleCreate(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID           string          `json:"id"`
		Participants []string        `json:"participants"`
		Payload      json.RawMessage `json:"payload"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ID == "" || len(req.Participants) == 0 {
		http.Error(w, "need {id, participants, payload}", http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	if _, exists := c.txs[req.ID]; exists {
		c.mu.Unlock()
		http.Error(w, "transaction exists", http.StatusConflict)
		return
	}
	c.mu.Unlock()

	// Durable first, visible second.
	if err := c.wal.Append(Record{TxID: req.ID, State: StatePreparing, Payload: req.Payload, Participants: req.Participants}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	CrashPoint("c:after-preparing")

	c.mu.Lock()
	c.txs[req.ID] = &Tx{ID: req.ID, Participants: req.Participants, Payload: req.Payload, State: StatePreparing}
	c.mu.Unlock()
	w.WriteHeader(http.StatusCreated)
	fmt.Fprintf(w, `{"id":%q,"state":%q}`+"\n", req.ID, StatePreparing)
}

func (c *Coordinator) handleCommit(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	c.mu.Lock()
	tx, ok := c.txs[id]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "unknown transaction", http.StatusNotFound)
		return
	}
	state, prevDecision := tx.State, tx.Decision
	c.mu.Unlock()
	if state != StatePreparing {
		// Already decided (or finished): idempotent answer.
		writeJSON(w, http.StatusOK, map[string]string{"id": id, "state": state, "decision": prevDecision})
		return
	}

	// Phase 1: ask every participant to prepare.
	allYes := true
	for _, p := range tx.Participants {
		if !c.prepare(p, tx) {
			allYes = false
			break
		}
	}
	CrashPoint("c:after-all-prepared")

	// The decision becomes durable BEFORE any participant is told.
	decision := StateCommit
	if !allYes {
		decision = StateAbort
	}
	if err := c.wal.Append(Record{TxID: id, State: decision}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	c.mu.Lock()
	tx.State = decision
	tx.Decision = decision
	c.mu.Unlock()
	CrashPoint("c:after-decision")

	// Phase 2: deliver the decision (synchronously; retries forever).
	c.driveDecision(tx)
	writeJSON(w, http.StatusOK, map[string]string{"id": id, "state": StateDone, "decision": decision})
}

func (c *Coordinator) handleAbort(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	c.mu.Lock()
	tx, ok := c.txs[id]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "unknown transaction", http.StatusNotFound)
		return
	}
	state, decision := tx.State, tx.Decision
	c.mu.Unlock()
	switch state {
	case StatePreparing:
		if err := c.wal.Append(Record{TxID: id, State: StateAbort}); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		c.mu.Lock()
		tx.State = StateAbort
		tx.Decision = StateAbort
		c.mu.Unlock()
		CrashPoint("c:after-decision")
		c.driveDecision(tx)
		writeJSON(w, http.StatusOK, map[string]string{"id": id, "state": StateDone, "decision": StateAbort})
	case StateCommit:
		http.Error(w, "commit decision already durable; cannot abort", http.StatusConflict)
	default:
		writeJSON(w, http.StatusOK, map[string]string{"id": id, "state": state, "decision": decision})
	}
}

func (c *Coordinator) handleGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	c.mu.Lock()
	tx, ok := c.txs[id]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "unknown transaction", http.StatusNotFound)
		return
	}
	state, decision := tx.State, tx.Decision
	participants := append([]string(nil), tx.Participants...)
	c.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"id":           id,
		"state":        state,
		"decision":     decision,
		"participants": participants,
	})
}

// prepare sends one prepare request; a yes vote is a 200.
func (c *Coordinator) prepare(p string, tx *Tx) bool {
	body, _ := json.Marshal(map[string]any{"txId": tx.ID, "payload": tx.Payload})
	resp, err := c.client.Post(p+"/prepare", "application/json", bytes.NewReader(body))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// driveDecision delivers the durable decision to every participant,
// retrying until all have acked, then logs DONE. It never gives up: the
// decision is final and participants are waiting (blocked) for it.
func (c *Coordinator) driveDecision(tx *Tx) {
	pending := map[string]bool{}
	for _, p := range tx.Participants {
		pending[p] = true
	}
	for len(pending) > 0 {
		for p := range pending {
			if c.notify(p, tx.Decision, tx.ID) {
				delete(pending, p)
			}
		}
		if len(pending) > 0 {
			select {
			case <-c.stopCh:
				return
			case <-time.After(c.retryInterval):
			}
		}
	}
	if err := c.wal.Append(Record{TxID: tx.ID, State: StateDone, Decision: tx.Decision}); err != nil {
		return
	}
	c.mu.Lock()
	tx.State = StateDone
	c.mu.Unlock()
	CrashPoint("c:after-done")
}

func (c *Coordinator) notify(p, decision, txID string) bool {
	path := "/commit"
	if decision == StateAbort {
		path = "/abort"
	}
	body, _ := json.Marshal(map[string]string{"txId": txID})
	resp, err := c.client.Post(p+path, "application/json", bytes.NewReader(body))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}
