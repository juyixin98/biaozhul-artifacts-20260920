package tpc

import (
	"encoding/json"
	"net/http"
	"sync"
	"time"
)

// PTx is a participant's in-memory view of one transaction.
type PTx struct {
	ID       string
	State    string // PREPARED, COMMITTED, ABORTED
	Payload  json.RawMessage
	Resource string // locked while PREPARED
}

// Participant votes and applies decisions. Its WAL is authoritative.
//
// Blocking behaviour: once PREPARED is durable the participant has promised
// to be able to commit. It must NOT unilaterally abort on a timeout — the
// coordinator may already have decided COMMIT. Instead the transaction
// stays PREPARED (its resource stays locked, i.e. the participant is
// *blocked*) and a recovery loop polls the coordinator until it learns the
// decision. Only the coordinator's durable decision ends a prepared
// transaction.
type Participant struct {
	wal          *WAL
	mu           sync.Mutex
	txs          map[string]*PTx
	locks        map[string]string // resource -> txID holding the lock
	coordURL     string
	client       *http.Client
	pollInterval time.Duration
	stopCh       chan struct{}
	wg           sync.WaitGroup
}

// NewParticipant opens (or recovers) a participant. coordURL is used by the
// recovery loop to ask the coordinator for the decision of any transaction
// left PREPARED by a crash. pollInterval <= 0 selects a default.
func NewParticipant(walPath, coordURL string, pollInterval time.Duration) (*Participant, error) {
	if pollInterval <= 0 {
		pollInterval = 200 * time.Millisecond
	}
	wal, recs, err := OpenWAL(walPath)
	if err != nil {
		return nil, err
	}
	p := &Participant{
		wal:          wal,
		txs:          map[string]*PTx{},
		locks:        map[string]string{},
		coordURL:     coordURL,
		client:       &http.Client{Timeout: 2 * time.Second},
		pollInterval: pollInterval,
		stopCh:       make(chan struct{}),
	}
	for _, r := range recs {
		switch r.State {
		case StatePrepared:
			p.txs[r.TxID] = &PTx{ID: r.TxID, State: StatePrepared, Payload: r.Payload, Resource: payloadResource(r.Payload)}
		case StateCommitted:
			p.txs[r.TxID] = &PTx{ID: r.TxID, State: StateCommitted}
		case StateAborted:
			p.txs[r.TxID] = &PTx{ID: r.TxID, State: StateAborted}
		}
	}
	// Recovery: re-acquire locks and resolve every interrupted PREPARED tx.
	for _, tx := range p.txs {
		if tx.State == StatePrepared {
			if tx.Resource != "" {
				p.locks[tx.Resource] = tx.ID
			}
			p.wg.Add(1)
			go p.resolveLoop(tx.ID)
		}
	}
	return p, nil
}

// Handler returns the participant's HTTP API:
//
//	POST /prepare  {"txId","payload":{...}} -> 200 yes / 409 no
//	POST /commit   {"txId"}                  -> 200 (idempotent)
//	POST /abort    {"txId"}                  -> 200 (idempotent)
//	GET  /status   all known transactions, with blocked flags
func (p *Participant) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /prepare", p.handlePrepare)
	mux.HandleFunc("POST /commit", p.handleCommit)
	mux.HandleFunc("POST /abort", p.handleAbort)
	mux.HandleFunc("GET /status", p.handleStatus)
	return mux
}

// Close stops recovery loops and closes the log.
func (p *Participant) Close() error {
	close(p.stopCh)
	p.wg.Wait()
	return p.wal.Close()
}

func (p *Participant) handlePrepare(w http.ResponseWriter, r *http.Request) {
	var req struct {
		TxID    string          `json:"txId"`
		Payload json.RawMessage `json:"payload"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TxID == "" {
		http.Error(w, "need {txId, payload}", http.StatusBadRequest)
		return
	}
	resource := payloadResource(req.Payload)
	voteNo := payloadVote(req.Payload) == "no"

	p.mu.Lock()
	if tx, ok := p.txs[req.TxID]; ok {
		defer p.mu.Unlock()
		if tx.State == StatePrepared {
			writeJSON(w, http.StatusOK, map[string]string{"vote": "yes"}) // idempotent retry
		} else {
			http.Error(w, "transaction already "+tx.State, http.StatusConflict)
		}
		return
	}
	if voteNo {
		p.mu.Unlock()
		http.Error(w, "vote no", http.StatusConflict)
		return
	}
	if resource != "" {
		if holder, locked := p.locks[resource]; locked && holder != req.TxID {
			p.mu.Unlock()
			http.Error(w, "resource "+resource+" locked by "+holder, http.StatusConflict)
			return
		}
	}
	// The yes vote is a promise: make PREPARED durable before replying.
	if err := p.wal.Append(Record{TxID: req.TxID, State: StatePrepared, Payload: req.Payload}); err != nil {
		p.mu.Unlock()
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	p.txs[req.TxID] = &PTx{ID: req.TxID, State: StatePrepared, Payload: req.Payload, Resource: resource}
	if resource != "" {
		p.locks[resource] = req.TxID
	}
	p.mu.Unlock()

	CrashPoint("p:after-prepared")
	writeJSON(w, http.StatusOK, map[string]string{"vote": "yes"})
}

func (p *Participant) handleCommit(w http.ResponseWriter, r *http.Request) {
	txID, ok := decodeTxID(w, r)
	if !ok {
		return
	}
	code, err := p.applyCommit(txID)
	if err != nil {
		http.Error(w, err.Error(), code)
		return
	}
	CrashPoint("p:after-committed")
	writeJSON(w, http.StatusOK, map[string]string{"txId": txID, "state": StateCommitted})
}

func (p *Participant) handleAbort(w http.ResponseWriter, r *http.Request) {
	txID, ok := decodeTxID(w, r)
	if !ok {
		return
	}
	code, err := p.applyAbort(txID)
	if err != nil {
		http.Error(w, err.Error(), code)
		return
	}
	CrashPoint("p:after-aborted")
	writeJSON(w, http.StatusOK, map[string]string{"txId": txID, "state": StateAborted})
}

func (p *Participant) handleStatus(w http.ResponseWriter, r *http.Request) {
	p.mu.Lock()
	defer p.mu.Unlock()
	txs := map[string]any{}
	for id, tx := range p.txs {
		txs[id] = map[string]any{
			"state":    tx.State,
			"blocked":  tx.State == StatePrepared, // waiting on the coordinator's decision
			"resource": tx.Resource,
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"txs": txs})
}

// applyCommit makes COMMITTED durable and releases the lock. Idempotent.
func (p *Participant) applyCommit(txID string) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	tx, ok := p.txs[txID]
	if !ok {
		return http.StatusNotFound, errString("unknown transaction")
	}
	switch tx.State {
	case StateCommitted:
		return 0, nil
	case StateAborted:
		return http.StatusConflict, errString("transaction already ABORTED")
	}
	if err := p.wal.Append(Record{TxID: txID, State: StateCommitted}); err != nil {
		return http.StatusInternalServerError, err
	}
	tx.State = StateCommitted
	if tx.Resource != "" {
		delete(p.locks, tx.Resource)
	}
	return 0, nil
}

// applyAbort makes ABORTED durable and releases the lock. Idempotent, and
// valid even for a transaction we never prepared (the prepare may have been
// lost before our own crash).
func (p *Participant) applyAbort(txID string) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	tx, ok := p.txs[txID]
	if ok && tx.State == StateCommitted {
		return http.StatusConflict, errString("transaction already COMMITTED")
	}
	if ok && tx.State == StateAborted {
		return 0, nil
	}
	if err := p.wal.Append(Record{TxID: txID, State: StateAborted}); err != nil {
		return http.StatusInternalServerError, err
	}
	if ok && tx.Resource != "" {
		delete(p.locks, tx.Resource)
	}
	p.txs[txID] = &PTx{ID: txID, State: StateAborted}
	return 0, nil
}

// resolveLoop is the recovery path for a transaction left PREPARED across a
// restart. It asks the coordinator for the decision and applies it.
//
// It deliberately has NO timeout: a prepared participant may never abort
// unilaterally, because the coordinator may have decided COMMIT. While the
// coordinator is unreachable the transaction stays PREPARED and blocked.
func (p *Participant) resolveLoop(txID string) {
	defer p.wg.Done()
	for {
		select {
		case <-p.stopCh:
			return
		case <-time.After(p.pollInterval):
		}
		decision := p.queryDecision(txID)
		switch decision {
		case StateCommit:
			p.applyCommit(txID)
			return
		case StateAbort:
			p.applyAbort(txID)
			return
		default:
			// Coordinator unreachable or still undecided: keep waiting.
		}
	}
}

// queryDecision asks the coordinator for the transaction's decision.
// Returns "" when there is no decision yet or the coordinator is down.
func (p *Participant) queryDecision(txID string) string {
	if p.coordURL == "" {
		return ""
	}
	resp, err := p.client.Get(p.coordURL + "/tx/" + txID)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	var body struct {
		State    string `json:"state"`
		Decision string `json:"decision"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return ""
	}
	switch body.State {
	case StateCommit, StateAbort:
		return body.State
	case StateDone:
		return body.Decision
	}
	return ""
}

func decodeTxID(w http.ResponseWriter, r *http.Request) (string, bool) {
	var req struct {
		TxID string `json:"txId"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TxID == "" {
		http.Error(w, "need {txId}", http.StatusBadRequest)
		return "", false
	}
	return req.TxID, true
}

func payloadResource(payload json.RawMessage) string {
	var v struct {
		Resource string `json:"resource"`
	}
	json.Unmarshal(payload, &v)
	return v.Resource
}

func payloadVote(payload json.RawMessage) string {
	var v struct {
		Vote string `json:"vote"`
	}
	json.Unmarshal(payload, &v)
	return v.Vote
}

type errString string

func (e errString) Error() string { return string(e) }
