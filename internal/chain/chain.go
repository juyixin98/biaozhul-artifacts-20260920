// Package chain implements the append-only evidence hash chain.
//
// Every event stores:
//   - Seq: per-case contiguous ordinal starting at 1;
//   - PrevDigest: entry digest of the previous event (genesis = SHA256(""));
//   - ContentDigest: SHA-256 of the canonical serialization of
//     {type, actor, payload, seq} — payload is re-canonicalized so a mutated
//     database row cannot keep its old digest;
//   - EntryDigest: SHA-256 over the canonical fixed-order fields
//     {seq, type, actor, prev_digest, content_digest, created_at_unix_nanos}.
//
// Appends are serialized per case by an insert-on-key advisory lock row,
// which works identically on MySQL/InnoDB and SQLite: concurrent appenders
// cannot create two events with the same predecessor (a fork).
package chain

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"forensiccore/internal/models"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// Genesis is the predecessor digest of the first event in every case.
var Genesis = hex.EncodeToString(sha256.New().Sum(nil)) // SHA-256 of empty input

// Errors returned by Verify.
var (
	ErrNoEvents    = errors.New("chain has no events")
	ErrBrokenChain = errors.New("chain verification failed")
)

// canonicalJSON serializes v deterministically: map keys sorted, no HTML
// escaping, compact separators. Numbers in payloads must be passed as the
// types JSON round-trips exactly (int64/float64/string/bool/nil/slice/map).
func canonicalJSON(v any) ([]byte, error) {
	// Round-trip through json first to normalize into map/[]any primitives.
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var decoded any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&decoded); err != nil {
		return nil, err
	}
	var buf []byte
	if err := encodeCanonical(&buf, decoded); err != nil {
		return nil, err
	}
	return buf, nil
}

func encodeCanonical(buf *[]byte, v any) error {
	switch t := v.(type) {
	case nil:
		*buf = append(*buf, "null"...)
	case string:
		b, err := json.Marshal(t)
		if err != nil {
			return err
		}
		*buf = append(*buf, b...)
	case json.Number:
		*buf = append(*buf, t.String()...)
	case bool:
		if t {
			*buf = append(*buf, "true"...)
		} else {
			*buf = append(*buf, "false"...)
		}
	case []any:
		*buf = append(*buf, '[')
		for i, e := range t {
			if i > 0 {
				*buf = append(*buf, ',')
			}
			if err := encodeCanonical(buf, e); err != nil {
				return err
			}
		}
		*buf = append(*buf, ']')
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		*buf = append(*buf, '{')
		for i, k := range keys {
			if i > 0 {
				*buf = append(*buf, ',')
			}
			kb, err := json.Marshal(k)
			if err != nil {
				return err
			}
			*buf = append(*buf, kb...)
			*buf = append(*buf, ':')
			if err := encodeCanonical(buf, t[k]); err != nil {
				return err
			}
		}
		*buf = append(*buf, '}')
	default:
		return fmt.Errorf("unsupported canonical type %T", v)
	}
	return nil
}

func hashHex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// ComputeDigests derives content and entry digests for an event. The created
// time is truncated to millisecond precision because MySQL stores DATETIME(3):
// hashing full nanoseconds would never match a round-tripped row.
func ComputeDigests(seq int, typ, actor string, payload []byte, prevDigest string, createdAt time.Time) (contentDigest, entryDigest string, err error) {
	createdAt = createdAt.UTC().Truncate(time.Millisecond)
	var payloadValue any
	if len(payload) > 0 {
		dec := json.NewDecoder(bytes.NewReader(payload))
		dec.UseNumber()
		if err := dec.Decode(&payloadValue); err != nil {
			return "", "", fmt.Errorf("payload is not valid JSON: %w", err)
		}
	}
	contentCanon, err := canonicalJSON(map[string]any{
		"seq":     json.Number(fmt.Sprintf("%d", seq)),
		"type":    typ,
		"actor":   actor,
		"payload": payloadValue,
	})
	if err != nil {
		return "", "", err
	}
	contentDigest = hashHex(contentCanon)
	entryCanon, err := canonicalJSON(map[string]any{
		"seq":              json.Number(fmt.Sprintf("%d", seq)),
		"type":             typ,
		"actor":            actor,
		"prev_digest":      prevDigest,
		"content_digest":   contentDigest,
		"created_at_nanos": json.Number(fmt.Sprintf("%d", createdAt.UnixNano())),
	})
	if err != nil {
		return "", "", err
	}
	entryDigest = hashHex(entryCanon)
	return contentDigest, entryDigest, nil
}

// Fault describes a chain integrity problem.
type Fault struct {
	Seq    int    `json:"seq"`
	Code   string `json:"code"` // MISSING | OUT_OF_ORDER | TAMPERED | FORK
	Detail string `json:"detail"`
}

const (
	FaultMissing    = "MISSING"
	FaultOutOfOrder = "OUT_OF_ORDER"
	FaultTampered   = "TAMPERED"
)

// VerifyResult is the outcome of a chain verification.
type VerifyResult struct {
	CaseID     uint    `json:"case_id"`
	OK         bool    `json:"ok"`
	EventCount int     `json:"event_count"`
	LastDigest string  `json:"last_digest"`
	Faults     []Fault `json:"faults,omitempty"`
}

// Verify checks contiguity, ordering, predecessor links and recomputes every
// digest over the stored rows. It detects gaps, reordering and tampering with
// either payload or digest columns.
func Verify(db *gorm.DB, caseID uint) (VerifyResult, error) {
	var events []models.ChainEvent
	if err := db.Where("case_id = ?", caseID).Order("seq ASC").Find(&events).Error; err != nil {
		return VerifyResult{}, err
	}
	res := VerifyResult{CaseID: caseID, EventCount: len(events)}
	if len(events) == 0 {
		return res, ErrNoEvents
	}
	prev := Genesis
	for i, e := range events {
		expectedSeq := i + 1
		if e.Seq != expectedSeq {
			if e.Seq < expectedSeq {
				// Duplicate ordinal: two rows claim the same position (fork/dup).
				res.Faults = append(res.Faults, Fault{Seq: e.Seq, Code: FaultOutOfOrder,
					Detail: fmt.Sprintf("duplicate ordinal %d at position %d", e.Seq, expectedSeq)})
			} else {
				res.Faults = append(res.Faults, Fault{Seq: expectedSeq, Code: FaultMissing,
					Detail: fmt.Sprintf("expected seq %d, found %d", expectedSeq, e.Seq)})
			}
		}
		if e.PrevDigest != prev {
			res.Faults = append(res.Faults, Fault{Seq: e.Seq, Code: FaultTampered,
				Detail: fmt.Sprintf("prev_digest mismatch: stored %s, expected %s", e.PrevDigest, prev)})
		}
		contentDigest, entryDigest, err := ComputeDigests(e.Seq, e.Type, e.Actor, e.Payload, e.PrevDigest, e.CreatedAt)
		if err != nil {
			res.Faults = append(res.Faults, Fault{Seq: e.Seq, Code: FaultTampered, Detail: err.Error()})
			continue
		}
		if contentDigest != e.ContentDigest {
			res.Faults = append(res.Faults, Fault{Seq: e.Seq, Code: FaultTampered,
				Detail: "content digest mismatch (payload tampered)"})
		}
		if entryDigest != e.EntryDigest {
			res.Faults = append(res.Faults, Fault{Seq: e.Seq, Code: FaultTampered,
				Detail: "entry digest mismatch"})
		}
		prev = e.EntryDigest
	}
	res.OK = len(res.Faults) == 0
	res.LastDigest = prev
	return res, nil
}

// AppendOptions carries one new chain event.
type AppendOptions struct {
	CaseID  uint
	Type    string
	Actor   string
	Payload any // JSON-serializable; canonicalized for the content digest
}

// staleLockTTL lets a crashed holder's lock be taken over.
const staleLockTTL = 30 * time.Second

// ErrLockBusy means another append currently holds the per-case lock; the
// caller should retry (Append retries internally).
var ErrLockBusy = errors.New("chain lock busy")

// Append serializes appends for one case with an insert-on-key advisory lock,
// computes the next ordinal and all digests inside the locked transaction,
// and writes the event. Concurrent callers block/retry rather than forking.
func Append(db *gorm.DB, opts AppendOptions) (*models.ChainEvent, error) {
	payload, err := validateAndMarshal(opts)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(15 * time.Second)
	for {
		var event *models.ChainEvent
		err := db.Transaction(func(tx *gorm.DB) error {
			ev, aerr := appendInTx(tx, opts, payload)
			if aerr != nil {
				return aerr
			}
			event = ev
			return nil
		})
		switch {
		case err == nil:
			return event, nil
		case errors.Is(err, ErrLockBusy):
			if time.Now().After(deadline) {
				return nil, fmt.Errorf("chain lock contention for case %d", opts.CaseID)
			}
			time.Sleep(20 * time.Millisecond)
		default:
			return nil, err
		}
	}
}

// AppendTx appends an event inside the caller's existing transaction without
// beginning or committing one. It returns ErrLockBusy if another holder owns
// the per-case lock. Registration uses this so the case row and its genesis
// event commit atomically (genesis appends cannot contend with same-case
// appenders, since the case row does not exist until the transaction commits).
func AppendTx(tx *gorm.DB, opts AppendOptions) (*models.ChainEvent, error) {
	payload, err := validateAndMarshal(opts)
	if err != nil {
		return nil, err
	}
	return appendInTx(tx, opts, payload)
}

func validateAndMarshal(opts AppendOptions) ([]byte, error) {
	if opts.Type == "" || opts.Actor == "" {
		return nil, errors.New("type and actor are required")
	}
	payload, err := json.Marshal(opts.Payload)
	if err != nil {
		return nil, fmt.Errorf("marshal payload: %w", err)
	}
	if !json.Valid(payload) {
		return nil, errors.New("payload must be valid JSON")
	}
	return payload, nil
}

// appendInTx performs the locked append within an existing transaction.
func appendInTx(tx *gorm.DB, opts AppendOptions, payload []byte) (*models.ChainEvent, error) {
	now := time.Now()
	holder := fmt.Sprintf("%s-%d", opts.Actor, now.UnixNano())

	// Take over stale locks left by a crashed process.
	if err := tx.Where("case_id = ? AND expires_at < ?", opts.CaseID, now).
		Delete(&models.ChainLock{}).Error; err != nil {
		return nil, err
	}
	lock := models.ChainLock{
		CaseID:    opts.CaseID,
		Holder:    holder,
		ExpiresAt: now.Add(staleLockTTL),
	}
	res := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&lock)
	if res.Error != nil {
		return nil, res.Error
	}
	if res.RowsAffected == 0 {
		// Primary key already existed: someone else holds the lock.
		return nil, ErrLockBusy
	}

	var last models.ChainEvent
	err := tx.Where("case_id = ?", opts.CaseID).Order("seq DESC").Take(&last).Error
	var prev string
	switch {
	case errors.Is(err, gorm.ErrRecordNotFound):
		prev = Genesis
	case err != nil:
		return nil, err
	default:
		prev = last.EntryDigest
	}
	nextSeq := 1
	if err == nil {
		nextSeq = last.Seq + 1
	}

	createdAt := time.Now().UTC().Truncate(time.Millisecond)
	contentDigest, entryDigest, err := ComputeDigests(nextSeq, opts.Type, opts.Actor, payload, prev, createdAt)
	if err != nil {
		return nil, err
	}
	event := models.ChainEvent{
		CaseID:        opts.CaseID,
		Seq:           nextSeq,
		Type:          opts.Type,
		Actor:         opts.Actor,
		Payload:       payload,
		PrevDigest:    prev,
		ContentDigest: contentDigest,
		EntryDigest:   entryDigest,
		CreatedAt:     createdAt,
	}
	if err := tx.Create(&event).Error; err != nil {
		return nil, err
	}
	if err := tx.Delete(&models.ChainLock{CaseID: opts.CaseID}).Error; err != nil {
		return nil, err
	}
	return &event, nil
}

// List returns all events of a case in chain order.
func List(db *gorm.DB, caseID uint) ([]models.ChainEvent, error) {
	var events []models.ChainEvent
	err := db.Where("case_id = ?", caseID).Order("seq ASC").Find(&events).Error
	return events, err
}
