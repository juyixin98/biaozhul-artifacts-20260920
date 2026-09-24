// Package storage persists holding registers and cryptographically chained
// write receipts in SQLite. The register bank is the source of truth; every
// successful FC10 batch updates the bank and appends exactly one receipt in
// the same SQLite transaction, so a batch is either fully visible or not
// visible at all.
package storage

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

// MaxRegisterAddress is inclusive. Modbus register addresses are 16 bits,
// i.e. 0..65535; a deployment narrows the configured bank via NewStore.
const MaxRegisterAddress = 65535

var (
	// ErrOutOfRange means the requested [start, start+qty) does not fit the
	// configured bank. On FC10 the whole batch fails (exception 0x02).
	ErrOutOfRange = errors.New("storage: address range outside the register bank")
)

// Receipt is the non-repudiation record of one committed FC10 write batch.
//
// Hash chains receipts: Hash = SHA256(PrevHash || canonical record bytes).
// Tampering with any row breaks every subsequent hash, verifiable offline via
// VerifyChain. Note what this is and is not: the chain authenticates the
// server's append-only journal against silent local tampering. It is NOT a
// distributed/transactional guarantee and NOT an idempotency key — Modbus/TCP
// itself has no cross-connection deduplication, and the TransactionID is not
// unique (see README "Protocol behaviour, honestly").
type Receipt struct {
	Seq           int64
	TransactionID uint16
	UnitID        byte
	StartAddress  uint16
	Quantity      uint16
	Values        []uint16
	ClientAddr    string
	CommittedAt   time.Time
	PrevHash      string
	Hash          string
}

// Store is the register bank plus the receipt journal.
//
// Concurrency: a single RWMutex serialises writers but lets readers run
// concurrently, and every read/write is one SQLite transaction. Readers
// therefore observe either the pre-batch or the post-batch bank, never a
// half-applied batch.
type Store struct {
	db *sql.DB

	mu sync.RWMutex
	// bankSize is the number of configured registers, addresses 0..bankSize-1.
	bankSize int
	// allowedUnits is the set of Unit IDs this gateway answers. Empty means
	// the callers don't enforce (server enforces explicitly instead).
	allowedUnits map[byte]bool
}

// Options configures a Store.
type Options struct {
	DSN          string // e.g. "file:data/modbus.db?cache=shared"
	BankSize     int    // number of holding registers (1..65536)
	AllowedUnits []byte // accepted Unit IDs
	ReadOnly     bool   // open an existing database read-only; skip DDL/PRAGMAs
}

// NewStore opens (or creates) the SQLite database and initialises the schema.
func NewStore(ctx context.Context, opts Options) (*Store, error) {
	if opts.BankSize < 1 || opts.BankSize > MaxRegisterAddress+1 {
		return nil, fmt.Errorf("storage: bank size %d out of range 1..%d", opts.BankSize, MaxRegisterAddress+1)
	}
	db, err := sql.Open("sqlite", opts.DSN)
	if err != nil {
		return nil, err
	}
	// One connection: modernc.org/sqlite + shared cache would otherwise be
	// possible too, but WAL below already gives concurrent readers and a
	// single connection makes transaction semantics here dead simple and
	// free of "database is locked" surprises under write contention.
	db.SetMaxOpenConns(1)

	s := &Store{db: db, bankSize: opts.BankSize, allowedUnits: map[byte]bool{}}
	for _, u := range opts.AllowedUnits {
		s.allowedUnits[u] = true
	}
	if !opts.ReadOnly {
		if err := s.init(ctx); err != nil {
			db.Close()
			return nil, err
		}
	}
	return s, nil
}

func (s *Store) init(ctx context.Context) error {
	pragmas := []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA foreign_keys=ON",
		"PRAGMA busy_timeout=5000",
		"PRAGMA synchronous=FULL",
	}
	for _, p := range pragmas {
		if _, err := s.db.ExecContext(ctx, p); err != nil {
			return fmt.Errorf("storage: %s: %w", p, err)
		}
	}
	const schema = `
CREATE TABLE IF NOT EXISTS registers (
	address INTEGER PRIMARY KEY CHECK (address >= 0 AND address <= 65535),
	value   INTEGER NOT NULL CHECK (value >= 0 AND value <= 65535)
);
CREATE TABLE IF NOT EXISTS receipts (
	seq             INTEGER PRIMARY KEY AUTOINCREMENT,
	transaction_id  INTEGER NOT NULL,
	unit_id         INTEGER NOT NULL,
	start_address   INTEGER NOT NULL,
	quantity        INTEGER NOT NULL,
	value_blob      BLOB    NOT NULL,           -- 2*quantity bytes, big-endian
	client_addr     TEXT    NOT NULL,
	committed_at    TEXT    NOT NULL,           -- RFC3339Nano UTC
	prev_hash       TEXT    NOT NULL,
	hash            TEXT    NOT NULL
);
`
	if _, err := s.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("storage: schema: %w", err)
	}
	return nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// BankSize reports the configured number of holding registers.
func (s *Store) BankSize() int { return s.bankSize }

// UnitAllowed reports whether the gateway is configured to answer unitID.
func (s *Store) UnitAllowed(unitID byte) bool { return s.allowedUnits[unitID] }

func (s *Store) inRange(start, qty uint16) bool {
	if qty == 0 {
		return false
	}
	last := uint32(start) + uint32(qty) - 1
	return last < uint32(s.bankSize)
}

// Read returns qty registers starting at start. Uninitialised registers read
// as zero (Modbus holds no separate "uninitialised" notion). Returns
// ErrOutOfRange if the window exceeds the bank.
func (s *Store) Read(ctx context.Context, start, qty uint16) ([]uint16, error) {
	if !s.inRange(start, qty) {
		return nil, ErrOutOfRange
	}
	s.mu.RLock()
	defer s.mu.RUnlock()

	tx, err := s.db.BeginTx(ctx, nil) // default deferred tx -> read snapshot
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() //nolint:errcheck // read-only tx

	out := make([]uint16, qty)
	rows, err := tx.QueryContext(ctx,
		`SELECT address, value FROM registers WHERE address >= ? AND address < ?`,
		start, uint32(start)+uint32(qty))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var addr, val int64
		if err := rows.Scan(&addr, &val); err != nil {
			return nil, err
		}
		out[addr-int64(start)] = uint16(val)
	}
	return out, rows.Err()
}

// WriteResult is returned by Write: the new bank values plus the receipt.
type WriteResult struct {
	Values  []uint16
	Receipt Receipt
}

// Write applies one FC10 batch atomically: range check, register update and
// receipt insert all happen inside a single SQLite transaction. If ANY
// register is out of range the transaction is never started and no register
// changes (exception 0x02 for the whole batch).
//
// midWrite, when non-nil, is invoked while the write lock is held and the
// SQLite transaction is open: AFTER every register of the batch has been
// UPSERTed but BEFORE the receipt is inserted and the transaction committed.
// Because the transaction is uncommitted and the write lock is exclusive,
// concurrent readers block here and can never observe the partially UPSERTed
// batch. Tests use the hook to prove exactly that; production passes nil.
func (s *Store) Write(
	ctx context.Context,
	transactionID uint16,
	unitID byte,
	start uint16,
	values []uint16,
	clientAddr string,
	committedAt time.Time,
	midWrite func(),
) (WriteResult, error) {
	qty := uint16(len(values))
	if !s.inRange(start, qty) {
		return WriteResult{}, ErrOutOfRange
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return WriteResult{}, err
	}
	committed := false
	defer func() {
		if !committed {
			tx.Rollback() //nolint:errcheck
		}
	}()

	// UPSERT every register of the batch. If any statement fails the
	// rollback restores the pre-batch bank.
	stmt, err := tx.PrepareContext(ctx,
		`INSERT INTO registers(address, value) VALUES(?, ?)
		 ON CONFLICT(address) DO UPDATE SET value = excluded.value`)
	if err != nil {
		return WriteResult{}, err
	}
	for i, v := range values {
		addr := uint32(start) + uint32(i)
		if _, err := stmt.ExecContext(ctx, int64(addr), int64(v)); err != nil {
			stmt.Close()
			return WriteResult{}, err
		}
	}
	stmt.Close()

	if midWrite != nil {
		midWrite()
	}

	// Receipt, chained to the tip of the journal.
	var prevSeq sql.NullInt64
	var prevHash string
	if err := tx.QueryRowContext(ctx,
		`SELECT seq, hash FROM receipts ORDER BY seq DESC LIMIT 1`).
		Scan(&prevSeq, &prevHash); err != nil {
		if !errors.Is(err, sql.ErrNoRows) {
			return WriteResult{}, err
		}
		prevHash = "" // genesis
	}

	r := Receipt{
		TransactionID: transactionID,
		UnitID:        unitID,
		StartAddress:  start,
		Quantity:      qty,
		Values:        append([]uint16(nil), values...),
		ClientAddr:    clientAddr,
		CommittedAt:   committedAt.UTC(),
		PrevHash:      prevHash,
	}
	r.Hash = hashReceipt(prevHash, r)

	blob := valuesToBlob(values)
	res, err := tx.ExecContext(ctx,
		`INSERT INTO receipts
		   (transaction_id, unit_id, start_address, quantity, value_blob,
		    client_addr, committed_at, prev_hash, hash)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		int64(r.TransactionID), int64(r.UnitID), int64(r.StartAddress),
		int64(r.Quantity), blob, r.ClientAddr,
		r.CommittedAt.Format(time.RFC3339Nano), r.PrevHash, r.Hash)
	if err != nil {
		return WriteResult{}, err
	}
	r.Seq, err = res.LastInsertId()
	if err != nil {
		return WriteResult{}, err
	}

	if err := tx.Commit(); err != nil {
		return WriteResult{}, err
	}
	committed = true
	out := append([]uint16(nil), values...)
	return WriteResult{Values: out, Receipt: r}, nil
}

// ListReceipts returns up to limit receipts (newest last when ascending).
// limit <= 0 means all.
func (s *Store) ListReceipts(ctx context.Context, limit int) ([]Receipt, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	q := `SELECT seq, transaction_id, unit_id, start_address, quantity,
	             value_blob, client_addr, committed_at, prev_hash, hash
	      FROM receipts ORDER BY seq ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := s.db.QueryContext(ctx, q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Receipt
	for rows.Next() {
		r, err := scanReceipt(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// VerifyChain recomputes every receipt hash from the stored rows and returns
// the first broken sequence number, or -1 when the chain is intact.
func (s *Store) VerifyChain(ctx context.Context) (brokenAtSeq int64, err error) {
	receipts, err := s.ListReceipts(ctx, 0)
	if err != nil {
		return -1, err
	}
	prev := ""
	for _, r := range receipts {
		if r.PrevHash != prev {
			return r.Seq, nil
		}
		if r.Hash != hashReceipt(prev, r) {
			return r.Seq, nil
		}
		prev = r.Hash
	}
	return -1, nil
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanReceipt(sc rowScanner) (Receipt, error) {
	var r Receipt
	var txnID, unitID, start, qty int64
	var blob []byte
	var at string
	if err := sc.Scan(&r.Seq, &txnID, &unitID, &start, &qty, &blob,
		&r.ClientAddr, &at, &r.PrevHash, &r.Hash); err != nil {
		return Receipt{}, err
	}
	t, err := time.Parse(time.RFC3339Nano, at)
	if err != nil {
		return Receipt{}, fmt.Errorf("storage: receipt %d bad timestamp %q: %w", r.Seq, at, err)
	}
	if len(blob) != 2*int(qty) {
		return Receipt{}, fmt.Errorf("storage: receipt %d blob length %d != 2*%d", r.Seq, len(blob), qty)
	}
	r.TransactionID, r.UnitID = uint16(txnID), byte(unitID)
	r.StartAddress, r.Quantity = uint16(start), uint16(qty)
	r.Values = blobToValues(blob)
	r.CommittedAt = t
	return r, nil
}

// canonicalBytes is the exact byte sequence hashed for a receipt:
//
//	prevHash(hex) || committedAt(RFC3339Nano UTC, fixed width via the stored
//	string) || transactionID u16be || unitID u8 || start u16be || qty u16be
//	|| value_blob (big-endian register bytes) || clientAddr (UTF-8)
//
// The encoding is fixed-layout and unambiguous; it is documented here and in
// the README so third parties can recompute hashes independently.
func canonicalBytes(prevHash string, r Receipt) []byte {
	var buf []byte
	buf = append(buf, []byte(prevHash)...)
	buf = append(buf, []byte(r.CommittedAt.Format(time.RFC3339Nano))...)
	var head [7]byte
	binary.BigEndian.PutUint16(head[0:2], r.TransactionID)
	head[2] = r.UnitID
	binary.BigEndian.PutUint16(head[3:5], r.StartAddress)
	binary.BigEndian.PutUint16(head[5:7], r.Quantity)
	buf = append(buf, head[:]...)
	buf = append(buf, valuesToBlob(r.Values)...)
	buf = append(buf, []byte(r.ClientAddr)...)
	return buf
}

func hashReceipt(prevHash string, r Receipt) string {
	sum := sha256.Sum256(canonicalBytes(prevHash, r))
	return hex.EncodeToString(sum[:])
}

func valuesToBlob(values []uint16) []byte {
	b := make([]byte, 2*len(values))
	for i, v := range values {
		binary.BigEndian.PutUint16(b[2*i:], v)
	}
	return b
}

func blobToValues(b []byte) []uint16 {
	out := make([]uint16, len(b)/2)
	for i := range out {
		out[i] = binary.BigEndian.Uint16(b[2*i:])
	}
	return out
}
