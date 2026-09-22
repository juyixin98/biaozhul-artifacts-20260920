// Package auditchain implements the per-organization append-only audit chain.
//
// Each entry carries: an org-local monotonic sequence number, the hash of the
// previous entry, and a hash over the canonical JSON of its payload:
//
//	entry_hash = sha256(seq || "\n" || prev_hash || "\n" || sha256(canonical))
//
// Append takes a transaction-scoped advisory lock keyed by org, so concurrent
// appenders serialize and the chain cannot fork. Verification walks the chain
// and reports missing sequence numbers, hash mismatches (tampering), and
// ordering gaps against the stored head pointer.
package auditchain

import (
	"context"
	"errors"
	"fmt"

	"dams/internal/db"
	"dams/internal/platform/canonical"
	"dams/internal/platform/hash"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// LockKeyNS is the advisory-lock namespace for DAMS audit chains.
const LockKeyNS int64 = 0xD4A5

var ErrChainBroken = errors.New("audit chain verification failed")

type Entry struct {
	Type    string
	Actor   *Actor
	Payload any
}

type Actor struct {
	APIKeyID int64
	Label    string
}

type Service struct{}

func New() *Service { return &Service{} }

// Append serializes on the org chain lock and writes exactly one entry.
func (s *Service) Append(ctx context.Context, tx pgx.Tx, orgID int64, e Entry) (db.AuditEntry, error) {
	q := db.New(tx)

	// Transaction-scoped advisory lock: automatically released at commit/
	// rollback. All appenders for one org queue here; different orgs proceed
	// concurrently on different keys.
	if _, err := tx.Exec(ctx,
		"SELECT pg_advisory_xact_lock($1, $2)", LockKeyNS, orgID); err != nil {
		return db.AuditEntry{}, err
	}
	head, err := q.LockAuditChain(ctx, orgID)
	if err != nil {
		return db.AuditEntry{}, err
	}

	canon, err := canonical.JSON(e.Payload)
	if err != nil {
		return db.AuditEntry{}, err
	}
	seq := head.HeadSeq + 1
	h := hash.AuditEntryHash(seq, head.HeadHash, canon)

	var actorID pgtype.Int8
	label := ""
	if e.Actor != nil {
		if e.Actor.APIKeyID > 0 {
			actorID = pgtype.Int8{Int64: e.Actor.APIKeyID, Valid: true}
		}
		label = e.Actor.Label
	}
	row, err := q.InsertAuditEntry(ctx, db.InsertAuditEntryParams{
		OrgID:         orgID,
		Seq:           seq,
		EntryType:     e.Type,
		ActorApiKeyID: actorID,
		ActorLabel:    label,
		Payload:       canon,
		PrevHash:      head.HeadHash,
		EntryHash:     h,
	})
	if err != nil {
		return db.AuditEntry{}, err
	}
	if err := q.AdvanceAuditChain(ctx, db.AdvanceAuditChainParams{
		OrgID: orgID, HeadSeq: seq, HeadHash: h,
	}); err != nil {
		return db.AuditEntry{}, err
	}
	return row, nil
}

// Issue describes one verification failure.
type Issue struct {
	Kind    string `json:"kind"` // gap | tampered | broken_link | head_mismatch
	Seq     int64  `json:"seq,omitempty"`
	Message string `json:"message"`
}

type Report struct {
	OK              bool    `json:"ok"`
	EntriesChecked  int     `json:"entries_checked"`
	HeadSeq         int64   `json:"head_seq"`
	HeadHash        string  `json:"head_hash"`
	ExpectedHeadSeq int64   `json:"expected_head_seq,omitempty"`
	Issues          []Issue `json:"issues,omitempty"`
}

// Verify walks the whole chain for an org. Read-only.
func (s *Service) Verify(ctx context.Context, q *db.Queries, orgID int64) (Report, error) {
	rep := Report{OK: true}

	maxSeq, err := q.MaxAuditSeq(ctx, orgID)
	if err != nil {
		return rep, err
	}
	tip, err := q.AuditChainTip(ctx, orgID)
	if err != nil {
		if !errors.Is(err, pgx.ErrNoRows) {
			return rep, err
		}
		// No entry has ever been appended for this org: empty, valid chain.
		tip = db.AuditChainTipRow{HeadSeq: 0, HeadHash: hash.ZeroHash}
	}
	rep.ExpectedHeadSeq = tip.HeadSeq

	prevHash := hash.ZeroHash
	var expectedSeq int64 = 1
	const page = 500
	for {
		rows, err := q.ListAuditEntries(ctx, db.ListAuditEntriesParams{
			OrgID:   orgID,
			FromSeq: pgtype.Int8{Int64: expectedSeq, Valid: true},
			ToSeq:   pgtype.Int8{},
			Limit:   page,
		})
		if err != nil {
			return rep, err
		}
		if len(rows) == 0 {
			break
		}
		for _, row := range rows {
			// Missing sequence (rows deleted or never written): advance expected.
			for row.Seq > expectedSeq {
				rep.OK = false
				rep.Issues = append(rep.Issues, Issue{
					Kind: "gap", Seq: expectedSeq,
					Message: fmt.Sprintf("missing audit entry seq=%d", expectedSeq),
				})
				expectedSeq++
			}
			// Out of order is impossible with ORDER BY seq, but a forked chain
			// presents as a seq smaller than expected — flag it.
			if row.Seq < expectedSeq {
				rep.OK = false
				rep.Issues = append(rep.Issues, Issue{
					Kind: "out_of_order", Seq: row.Seq,
					Message: fmt.Sprintf("unexpected/duplicate seq=%d", row.Seq),
				})
			}

			canon, err := canonical.MarshalRoundTrip(row.Payload)
			if err != nil {
				rep.OK = false
				rep.Issues = append(rep.Issues, Issue{Kind: "tampered", Seq: row.Seq, Message: err.Error()})
			}
			want := hash.AuditEntryHash(row.Seq, row.PrevHash, canon)
			if row.PrevHash != prevHash {
				rep.OK = false
				rep.Issues = append(rep.Issues, Issue{
					Kind: "broken_link", Seq: row.Seq,
					Message: fmt.Sprintf("prev_hash mismatch at seq=%d", row.Seq),
				})
			}
			if want != row.EntryHash {
				rep.OK = false
				rep.Issues = append(rep.Issues, Issue{
					Kind: "tampered", Seq: row.Seq,
					Message: fmt.Sprintf("entry hash mismatch at seq=%d (payload modified?)", row.Seq),
				})
			}
			prevHash = row.EntryHash
			expectedSeq = row.Seq + 1
			rep.EntriesChecked++
		}
		if int64(len(rows)) < page {
			break
		}
	}

	if maxSeq != tip.HeadSeq {
		rep.OK = false
		rep.Issues = append(rep.Issues, Issue{
			Kind:    "head_mismatch",
			Message: fmt.Sprintf("max entry seq=%d but chain head=%d (uncommitted/forked tail)", maxSeq, tip.HeadSeq),
		})
	}
	if prevHash != tip.HeadHash && tip.HeadSeq > 0 {
		rep.OK = false
		rep.Issues = append(rep.Issues, Issue{
			Kind:    "head_mismatch",
			Message: "stored head hash does not match recomputed chain tip",
		})
	}
	rep.HeadSeq = tip.HeadSeq
	rep.HeadHash = tip.HeadHash
	return rep, nil
}
