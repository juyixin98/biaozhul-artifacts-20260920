// Package txn implements a small, self-contained transactional store used as
// the "local database" for the idempotency project. It depends only on the
// Go standard library.
//
// It provides snapshot transactions (MVCC-lite): readers work on a deep
// snapshot taken at Begin, writers validate against committed state at
// Commit. A commit is written to a write-ahead log as ONE framed,
// checksummed record and fsync'ed before the in-memory state is updated, so
// the idempotency key row, the business rows and the side-effect rows
// become visible atomically and survive process restarts.
package txn

import "time"

// KeyState is the lifecycle state of an idempotency key.
type KeyState string

const (
	// KeyProcessing means a request owns the key but has not committed a result.
	KeyProcessing KeyState = "processing"
	// KeyCompleted is terminal: the stored response is authoritative.
	KeyCompleted KeyState = "completed"
	// KeyFailed means the last attempt failed retryably; the key may be retried.
	KeyFailed KeyState = "failed"
)

// KeyRow is the persisted idempotency record.
type KeyRow struct {
	Key          string    `json:"key"`
	Method       string    `json:"method"`
	Path         string    `json:"path"`
	ReqHash      string    `json:"req_hash"`
	State        KeyState  `json:"state"`
	StatusCode   int       `json:"status_code,omitempty"`
	ResponseBody []byte    `json:"response_body,omitempty"`
	Attempts     int       `json:"attempts"`
	LockedAt     time.Time `json:"locked_at,omitempty"`
	CompletedAt  time.Time `json:"completed_at,omitempty"`
}

// Order is a business row created by POST /v1/orders.
type Order struct {
	ID        string    `json:"id"`
	Amount    int       `json:"amount"`
	Currency  string    `json:"currency"`
	Status    string    `json:"status"`
	IdemKey   string    `json:"idem_key"`
	CreatedAt time.Time `json:"created_at"`
}

// LedgerEntry is a side-effect row (a money movement). Side effects are
// append-only; the number of ledger entries carrying an idempotency key is
// exactly what the acceptance tests count.
type LedgerEntry struct {
	ID        int64     `json:"id"`
	Kind      string    `json:"kind"` // charge | coupon
	Detail    string    `json:"detail"`
	Amount    int       `json:"amount"`
	OrderID   string    `json:"order_id,omitempty"`
	IdemKey   string    `json:"idem_key"`
	CreatedAt time.Time `json:"created_at"`
}

// commitRecord is the single WAL record written for a business commit.
// Key row, orders and ledger entries in one record => atomic in storage.
type commitRecord struct {
	Key    KeyRow        `json:"key"`
	Orders []Order       `json:"orders,omitempty"`
	Ledger []LedgerEntry `json:"ledger,omitempty"`
}
