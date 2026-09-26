package txn

import (
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"idemresp/internal/clock"
)

// Common errors.
var (
	// ErrNotFound is returned when no key row exists.
	ErrNotFound = errors.New("txn: idempotency key not found")
	// ErrConflict means the same key was already seen with a different
	// request body (request digest mismatch).
	ErrConflict = errors.New("txn: idempotency key reused with a different request payload")
	// ErrInProgress means another request holding the key is still
	// processing (or its lease has not yet expired).
	ErrInProgress = errors.New("txn: request with this key is still processing")
)

// DB is the transactional store. A single mutex serializes writers; readers
// operate on snapshot copies. It is intentionally simple rather than
// high-throughput — correctness under concurrency is the point of this
// project.
type DB struct {
	mu  sync.Mutex
	wal *WAL

	// committed state
	keys   map[string]KeyRow
	orders map[string]Order
	ledger []LedgerEntry

	nextLedgerID int64

	clk clock.Clock
}

// Open opens (or creates) a store in dir and replays the WAL.
func Open(dir string, clk clock.Clock) (*DB, error) {
	if clk == nil {
		clk = clock.Real{}
	}
	wal, err := openWAL(dir)
	if err != nil {
		return nil, err
	}
	db := &DB{
		wal:    wal,
		keys:   make(map[string]KeyRow),
		orders: make(map[string]Order),
		clk:    clk,
	}
	if err := db.replay(); err != nil {
		_ = wal.Close()
		return nil, err
	}
	return db, nil
}

func (db *DB) replay() error {
	frames, err := readFrames(db.wal.f)
	if err != nil {
		return err
	}
	for _, fr := range frames {
		switch fr.recType {
		case recClaim:
			var k KeyRow
			if err := json.Unmarshal(fr.payload, &k); err != nil {
				return fmt.Errorf("replay claim: %w", err)
			}
			db.keys[k.Key] = k
		case recFailed:
			var k KeyRow
			if err := json.Unmarshal(fr.payload, &k); err != nil {
				return fmt.Errorf("replay failed: %w", err)
			}
			db.keys[k.Key] = k
		case recCommit:
			var c commitRecord
			if err := json.Unmarshal(fr.payload, &c); err != nil {
				return fmt.Errorf("replay commit: %w", err)
			}
			db.applyCommit(c)
		default:
			return fmt.Errorf("replay: unknown record type %q", fr.recType)
		}
	}
	return nil
}

func (db *DB) applyCommit(c commitRecord) {
	db.keys[c.Key.Key] = c.Key
	for _, o := range c.Orders {
		db.orders[o.ID] = o
	}
	db.ledger = append(db.ledger, c.Ledger...)
	for _, e := range c.Ledger {
		if e.ID > db.nextLedgerID {
			db.nextLedgerID = e.ID
		}
	}
}

// Close releases the WAL.
func (db *DB) Close() error { return db.wal.Close() }

// ---------------------------------------------------------------------------
// Snapshot reads
// ---------------------------------------------------------------------------

// Snapshot is an immutable deep copy of committed state as of a moment.
type Snapshot struct {
	Keys   map[string]KeyRow
	Orders map[string]Order
	Ledger []LedgerEntry
}

// Snapshot returns a consistent deep copy of current committed state.
func (db *DB) Snapshot() Snapshot {
	db.mu.Lock()
	defer db.mu.Unlock()
	s := Snapshot{
		Keys:   make(map[string]KeyRow, len(db.keys)),
		Orders: make(map[string]Order, len(db.orders)),
		Ledger: append([]LedgerEntry(nil), db.ledger...),
	}
	for k, v := range db.keys {
		s.Keys[k] = cloneKeyRow(v)
	}
	for k, v := range db.orders {
		s.Orders[k] = v
	}
	return s
}

// CountLedgerForKey reports how many committed side effects carry idemKey.
func (db *DB) CountLedgerForKey(idemKey string) int {
	db.mu.Lock()
	defer db.mu.Unlock()
	n := 0
	for _, e := range db.ledger {
		if e.IdemKey == idemKey {
			n++
		}
	}
	return n
}

func cloneKeyRow(k KeyRow) KeyRow {
	k.ResponseBody = append([]byte(nil), k.ResponseBody...)
	return k
}

// ---------------------------------------------------------------------------
// Idempotency state machine (the core protocol)
// ---------------------------------------------------------------------------

// AcquireResult tells the caller what to do after Acquire.
type AcquireResult struct {
	// Created is true when a fresh processing claim was made: the caller
	// MUST execute the business logic exactly once and then Complete/Fail.
	Created bool
	// Existing is set when Created is false.
	Existing KeyRow
}

// Acquire implements first-stage idempotency semantics:
//
//   - unknown key                    -> create a durable processing claim
//   - completed key, same hash       -> return stored response for replay
//   - processing key, same hash      -> ErrInProgress (concurrent duplicate)
//   - failed key, same hash          -> reclaim if lease expired, else in-progress
//   - any state, different hash      -> ErrConflict (same key, other body)
//
// The processing claim is fsynced to the WAL BEFORE business work starts,
// so a crash during work leaves a recoverable "processing" marker rather
// than silently allowing a second execution.
func (db *DB) Acquire(key, method, path, reqHash string, lease time.Duration) (AcquireResult, error) {
	db.mu.Lock()
	defer db.mu.Unlock()

	now := db.clk.Now()
	cur, ok := db.keys[key]
	if ok {
		if cur.ReqHash != reqHash {
			return AcquireResult{Existing: cur}, ErrConflict
		}
		switch cur.State {
		case KeyCompleted:
			return AcquireResult{Created: false, Existing: cloneKeyRow(cur)}, nil
		case KeyProcessing:
			if !now.Before(cur.LockedAt.Add(lease)) {
				break // lease expired: fall through to reclaim
			}
			return AcquireResult{Existing: cloneKeyRow(cur)}, ErrInProgress
		case KeyFailed:
			break // a failed attempt already returned; retry immediately
		}
	}

	attempts := 1
	if ok {
		attempts = cur.Attempts + 1
	}
	row := KeyRow{
		Key:      key,
		Method:   method,
		Path:     path,
		ReqHash:  reqHash,
		State:    KeyProcessing,
		Attempts: attempts,
		LockedAt: now,
	}
	if err := db.wal.Append(recClaim, row); err != nil {
		return AcquireResult{}, err
	}
	db.keys[key] = row
	return AcquireResult{Created: true, Existing: row}, nil
}

// Fail marks an in-progress key as retryably failed and persists that fact.
// A later Acquire with an expired lease may retry it.
func (db *DB) Fail(key string, execErr error) error {
	db.mu.Lock()
	defer db.mu.Unlock()
	cur, ok := db.keys[key]
	if !ok {
		return ErrNotFound
	}
	cur.State = KeyFailed
	// Surface of the failure is stored in the response body so operators
	// can see why an attempt failed; it is replaced on successful retry.
	cur.ResponseBody = []byte(execErr.Error())
	if err := db.wal.Append(recFailed, cur); err != nil {
		return err
	}
	db.keys[key] = cur
	return nil
}

// Commit is the single atomic business commit: the completed idempotency
// record (including the response to replay), new orders and side effects
// are written as ONE WAL record and applied together. Either all are
// visible after recovery or none are.
func (db *DB) Commit(key string, statusCode int, respBody []byte, orders []Order, ledger []LedgerEntryInput) error {
	db.mu.Lock()
	defer db.mu.Unlock()

	cur, ok := db.keys[key]
	if !ok || cur.State != KeyProcessing {
		return fmt.Errorf("txn: key %q is not in processing state", key)
	}
	now := db.clk.Now()
	cur.State = KeyCompleted
	cur.StatusCode = statusCode
	cur.ResponseBody = append([]byte(nil), respBody...)
	cur.CompletedAt = now

	fullOrders := make([]Order, 0, len(orders))
	for _, o := range orders {
		o.IdemKey = key
		if o.CreatedAt.IsZero() {
			o.CreatedAt = now
		}
		fullOrders = append(fullOrders, o)
	}
	fullLedger := make([]LedgerEntry, 0, len(ledger))
	for _, e := range ledger {
		db.nextLedgerID++
		fullLedger = append(fullLedger, LedgerEntry{
			ID:        db.nextLedgerID,
			Kind:      e.Kind,
			Detail:    e.Detail,
			Amount:    e.Amount,
			OrderID:   e.OrderID,
			IdemKey:   key,
			CreatedAt: now,
		})
	}

	rec := commitRecord{Key: cur, Orders: fullOrders, Ledger: fullLedger}
	if err := db.wal.Append(recCommit, rec); err != nil {
		return err
	}
	db.applyCommit(rec)
	return nil
}

// LedgerEntryInput is a side effect supplied by business logic at commit.
type LedgerEntryInput struct {
	Kind    string
	Detail  string
	Amount  int
	OrderID string
}
