// Package receipt persists Modbus write receipts in SQLite.
//
// Every successful FC10 write produces one receipt row. Rows form a
// tamper-evident chain:
//
//	prev[0]   = SHA-256("modbus-receipt-genesis-v1")
//	body      = "seq|txn|unit|addr|qty|valuesHex"
//	bodyHash  = SHA-256(body)
//	chainHash = SHA-256(prev[i] || bodyHash[i])      -- the receipt hash
//	mac       = HMAC-SHA256(key, prev[i] || bodyHash[i])
//
// Rejected requests (exceptions) are recorded in a separate audit table.
//
// The HMAC key is server-side: it authenticates the log to the operator
// who holds the key. It is deliberately NOT transmitted over Modbus — the
// Modbus protocol itself contains no cryptographic receipt mechanism, and
// this implementation does not pretend otherwise.
package receipt

import (
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"

	_ "modernc.org/sqlite" // pure-Go SQLite driver, no CGO
)

// GenesisSalt seeds the previous-hash of the first receipt.
const GenesisSalt = "modbus-receipt-genesis-v1"

// ChainVersion identifies the canonicalization used in this build.
const ChainVersion = 1

// Receipt is one durable write record.
type Receipt struct {
	Seq           int64
	TransactionID uint16
	UnitID        byte
	Address       uint16
	Quantity      uint16
	ValuesHex     string
	BodySHA       string // hex SHA-256 of canonical body
	PrevSHA       string // hex chain hash of previous receipt
	ChainSHA      string // hex chain hash of this receipt
	HMACSHA       string // hex HMAC-SHA256 over (prev||body)
	CreatedAt     string
}

// Event is one audited exception response.
type Event struct {
	ID            int64
	CreatedAt     string
	TransactionID uint16
	UnitID        byte
	Function      byte
	ExceptionCode byte
	Reason        string
}

// Store owns the SQLite database. A Go-level mutex serializes writers;
// SQLite itself is opened in WAL mode for concurrent reader access.
type Store struct {
	db  *sql.DB
	mu  sync.Mutex
	key []byte
}

// Open opens (creating if needed) the receipt database at path.
// hmacKey authenticates the chain; callers load it from a file/env.
func Open(path string, hmacKey []byte) (*Store, error) {
	if len(hmacKey) == 0 {
		return nil, errors.New("empty HMAC key; pass an explicit key (see --key-file / MODBUS_RECEIPT_KEY)")
	}
	// modernc.org/sqlite registers driver name "sqlite".
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// One connection keeps the write side deterministic; WAL lets external
	// readers (verifier, sqlite3 CLI) read concurrently.
	db.SetMaxOpenConns(1)
	for _, pragma := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA synchronous=FULL",
		"PRAGMA busy_timeout=5000",
		"PRAGMA foreign_keys=ON",
	} {
		if _, err := db.Exec(pragma); err != nil {
			db.Close()
			return nil, fmt.Errorf("pragma %q: %w", pragma, err)
		}
	}
	s := &Store{db: db, key: append([]byte(nil), hmacKey...)}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate() error {
	_, err := s.db.Exec(`
CREATE TABLE IF NOT EXISTS receipts (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    unit_id        INTEGER NOT NULL,
    address        INTEGER NOT NULL,
    quantity       INTEGER NOT NULL,
    values_hex     TEXT    NOT NULL,
    body_sha256    TEXT    NOT NULL,
    prev_sha256    TEXT    NOT NULL,
    chain_sha256   TEXT    NOT NULL UNIQUE,
    hmac_sha256    TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS exceptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    transaction_id INTEGER NOT NULL,
    unit_id        INTEGER NOT NULL,
    function       INTEGER NOT NULL,
    exception_code INTEGER NOT NULL,
    reason         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_txn ON receipts(transaction_id);
`)
	return err
}

// LoadKey reads the HMAC key from keyFile, or from the MODBUS_RECEIPT_KEY
// environment variable when keyFile is empty. Trailing newlines of a key
// file are stripped; embedded whitespace is preserved.
func LoadKey(keyFile string) ([]byte, error) {
	if keyFile != "" {
		b, err := os.ReadFile(keyFile)
		if err != nil {
			return nil, fmt.Errorf("reading key file: %w", err)
		}
		b = []byte(strings.TrimRight(string(b), "\r\n"))
		if len(b) == 0 {
			return nil, errors.New("key file is empty")
		}
		return b, nil
	}
	if k := os.Getenv("MODBUS_RECEIPT_KEY"); k != "" {
		return []byte(k), nil
	}
	return nil, nil
}

func valuesToHex(values []uint16) string {
	var b strings.Builder
	b.Grow(4 * len(values))
	for _, v := range values {
		fmt.Fprintf(&b, "%04x", v)
	}
	return b.String()
}

func sha256Hex(data string) string {
	sum := sha256.Sum256([]byte(data))
	return hex.EncodeToString(sum[:])
}

func hmacHex(key []byte, data string) string {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(data))
	return hex.EncodeToString(m.Sum(nil))
}

// canonicalBody is the single place defining receipt canonicalization;
// the verifier must reconstruct exactly this string.
func canonicalBody(seq int64, txn uint16, unit byte, addr, qty uint16, valuesHex string) string {
	return fmt.Sprintf("v%d|%d|%d|%d|%d|%d|%s",
		ChainVersion, seq, txn, unit, addr, qty, valuesHex)
}

// AppendReceipt writes one receipt inside the caller's transaction. It
// computes the chain link against the latest committed receipt, so it must
// run with the SQLite write transaction held.
func appendReceipt(tx *sql.Tx, key []byte, txn uint16, unit byte, addr uint16, values []uint16) (Receipt, error) {
	var prev string
	switch err := tx.QueryRow(`SELECT chain_sha256 FROM receipts ORDER BY seq DESC LIMIT 1`).Scan(&prev); {
	case errors.Is(err, sql.ErrNoRows):
		prev = sha256Hex(GenesisSalt)
	case err != nil:
		return Receipt{}, err
	}

	res, err := tx.Exec(`INSERT INTO receipts
		(transaction_id, unit_id, address, quantity, values_hex, body_sha256, prev_sha256, chain_sha256, hmac_sha256)
		VALUES (?, ?, ?, ?, '', '', ?, '', '')`,
		txn, unit, addr, len(values), prev)
	if err != nil {
		return Receipt{}, err
	}
	seq, _ := res.LastInsertId()
	vh := valuesToHex(values)
	body := canonicalBody(seq, txn, unit, addr, uint16(len(values)), vh)
	bodyHash := sha256Hex(body)
	chain := sha256Hex(prev + bodyHash)
	mac := hmacHex(key, prev+bodyHash)

	if _, err := tx.Exec(`UPDATE receipts
		SET values_hex=?, body_sha256=?, chain_sha256=?, hmac_sha256=? WHERE seq=?`,
		vh, bodyHash, chain, mac, seq); err != nil {
		return Receipt{}, err
	}

	var createdAt string
	if err := tx.QueryRow(`SELECT created_at FROM receipts WHERE seq=?`, seq).Scan(&createdAt); err != nil {
		return Receipt{}, err
	}
	return Receipt{
		Seq: seq, TransactionID: txn, UnitID: unit, Address: addr,
		Quantity: uint16(len(values)), ValuesHex: vh, BodySHA: bodyHash,
		PrevSHA: prev, ChainSHA: chain, HMACSHA: mac, CreatedAt: createdAt,
	}, nil
}

// WithWriteTx runs fn inside an immediate SQLite write transaction,
// committing on success and rolling back on error.
func (s *Store) WithWriteTx(fn func(tx *sql.Tx) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	if _, err := tx.Exec("SAVEPOINT sp0"); err != nil {
		tx.Rollback()
		return err
	}
	if err := fn(tx); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}

// RecordWrite persists exactly one write receipt. It is meant to be called
// inside register.WriteBatch's commit callback, via WithWriteTx.
func (s *Store) RecordWrite(tx *sql.Tx, txn uint16, unit byte, addr uint16, values []uint16) (Receipt, error) {
	return appendReceipt(tx, s.key, txn, unit, addr, values)
}

// RecordException audits a rejected request. Exception logging never
// blocks/affects the protocol response; a logging failure is returned to
// the caller for logging but the Modbus exception is still sent.
func (s *Store) RecordException(txn uint16, unit byte, fc, code byte, reason string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(`INSERT INTO exceptions
		(transaction_id, unit_id, function, exception_code, reason)
		VALUES (?, ?, ?, ?, ?)`, txn, unit, fc, code, reason)
	return err
}

// ChainHead returns the chain hash of the latest receipt, or the genesis
// hash if the log is empty.
func (s *Store) ChainHead() (string, int64, error) {
	var head string
	var seq int64
	err := s.db.QueryRow(`SELECT chain_sha256, seq FROM receipts ORDER BY seq DESC LIMIT 1`).Scan(&head, &seq)
	if errors.Is(err, sql.ErrNoRows) {
		return sha256Hex(GenesisSalt), 0, nil
	}
	return head, seq, err
}

// VerifyResult summarizes a chain verification run.
type VerifyResult struct {
	Count       int64
	HeadSHA     string
	FirstBroken int64  // seq of first bad record, 0 if intact
	Problem     string // human-readable detail when FirstBroken != 0
}

func (r VerifyResult) OK() bool { return r.FirstBroken == 0 }

// Verify recomputes every body hash, chain link and HMAC from the stored
// rows using key. Any mismatch (deleted/modified row, wrong key) is
// reported with the offending sequence number.
func Verify(path string, key []byte) (VerifyResult, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return VerifyResult{}, err
	}
	defer db.Close()
	if _, err := db.Exec("PRAGMA busy_timeout=5000"); err != nil {
		return VerifyResult{}, err
	}

	rows, err := db.Query(`
		SELECT seq, transaction_id, unit_id, address, quantity,
		       values_hex, body_sha256, prev_sha256, chain_sha256, hmac_sha256
		FROM receipts ORDER BY seq`)
	if err != nil {
		return VerifyResult{}, err
	}
	defer rows.Close()

	prev := sha256Hex(GenesisSalt)
	var count int64
	var lastChain string
	for rows.Next() {
		var r Receipt
		var txn, unit, addr, qty int64
		if err := rows.Scan(&r.Seq, &txn, &unit, &addr, &qty,
			&r.ValuesHex, &r.BodySHA, &r.PrevSHA, &r.ChainSHA, &r.HMACSHA); err != nil {
			return VerifyResult{}, err
		}
		count++
		lastChain = r.ChainSHA

		// seq must be contiguous starting at 1.
		if r.Seq != count {
			return VerifyResult{Count: count - 1, HeadSHA: lastChain, FirstBroken: r.Seq,
				Problem: fmt.Sprintf("non-contiguous sequence number: want %d, got %d (a row was deleted)", count, r.Seq)}, nil
		}

		body := canonicalBody(r.Seq, uint16(txn), byte(unit), uint16(addr), uint16(qty), r.ValuesHex)
		wantBody := sha256Hex(body)
		if r.BodySHA != wantBody {
			return VerifyResult{Count: count, HeadSHA: lastChain, FirstBroken: r.Seq,
				Problem: fmt.Sprintf("body hash mismatch: stored %s, recomputed %s (row payload was altered)", r.BodySHA, wantBody)}, nil
		}
		if r.PrevSHA != prev {
			return VerifyResult{Count: count, HeadSHA: lastChain, FirstBroken: r.Seq,
				Problem: fmt.Sprintf("prev-hash mismatch: stored %s, expected %s (chain link broken)", r.PrevSHA, prev)}, nil
		}
		wantChain := sha256Hex(prev + wantBody)
		if r.ChainSHA != wantChain {
			return VerifyResult{Count: count, HeadSHA: lastChain, FirstBroken: r.Seq,
				Problem: fmt.Sprintf("chain hash mismatch: stored %s, recomputed %s", r.ChainSHA, wantChain)}, nil
		}
		wantMAC := hmacHex(key, prev+wantBody)
		if !hmac.Equal([]byte(r.HMACSHA), []byte(wantMAC)) {
			return VerifyResult{Count: count, HeadSHA: lastChain, FirstBroken: r.Seq,
				Problem: "HMAC mismatch: wrong key or receipt was forged"}, nil
		}
		prev = wantChain
	}
	if err := rows.Err(); err != nil {
		return VerifyResult{}, err
	}
	return VerifyResult{Count: count, HeadSHA: prev}, nil
}

// RecentEvents lists the most recent audited exceptions, newest first.
func (s *Store) RecentEvents(limit int) ([]Event, error) {
	rows, err := s.db.Query(`
		SELECT id, created_at, transaction_id, unit_id, function, exception_code, reason
		FROM exceptions ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var e Event
		var txn, unit, fc, code int64
		if err := rows.Scan(&e.ID, &e.CreatedAt, &txn, &unit, &fc, &code, &e.Reason); err != nil {
			return nil, err
		}
		e.TransactionID, e.UnitID, e.Function, e.ExceptionCode = uint16(txn), byte(unit), byte(fc), byte(code)
		out = append(out, e)
	}
	return out, rows.Err()
}

// RecentReceipts lists the most recent receipts, newest first.
func (s *Store) RecentReceipts(limit int) ([]Receipt, error) {
	rows, err := s.db.Query(`
		SELECT seq, transaction_id, unit_id, address, quantity, values_hex,
		       body_sha256, prev_sha256, chain_sha256, hmac_sha256, created_at
		FROM receipts ORDER BY seq DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Receipt
	for rows.Next() {
		var r Receipt
		var txn, unit, addr, qty int64
		if err := rows.Scan(&r.Seq, &txn, &unit, &addr, &qty, &r.ValuesHex,
			&r.BodySHA, &r.PrevSHA, &r.ChainSHA, &r.HMACSHA, &r.CreatedAt); err != nil {
			return nil, err
		}
		r.TransactionID, r.UnitID, r.Address, r.Quantity = uint16(txn), byte(unit), uint16(addr), uint16(qty)
		out = append(out, r)
	}
	return out, rows.Err()
}
