// Package auditchain implements the per-organization append-only,
// hash-chained audit log.
//
// Chain definition (all hashes are hex-encoded SHA-256):
//
//	content_canonical = canonicalJSON(content)   // RFC 8785-style: object
//	                                             // keys sorted, no insignif-
//	                                             // icant whitespace
//	content_hash      = sha256(content_canonical)
//	preimage          = seq || "\n" || orgID || "\n" || action || "\n" ||
//	                     content_hash || "\n" || prev_hash || "\n"
//	entry_hash        = sha256(preimage)
//
// The first entry of an organization has prev_hash = "" (empty string).
//
// Appends take a per-organization PostgreSQL transaction-level advisory lock,
// so concurrent appenders are strictly serialized: the chain cannot fork and
// sequence numbers are gapless.
package auditchain

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	"github.com/jackc/pgx/v5"

	sqlcgen "dams.local/dams/internal/db/sqlc"
)

// advisoryNamespace is the ASCII uint32 of "DAMS".
const advisoryNamespace int32 = 0x44414d53

// canonicalJSON re-serializes arbitrary JSON so that the digest is
// independent of key ordering and whitespace. Numbers round-trip through
// json.Number so integer values are not reformatted via float64.
func canonicalJSON(raw json.RawMessage) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("canonicalize: %w", err)
	}
	out, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return out, nil
}

// CanonicalHash returns the content hash stored on an audit entry.
func CanonicalHash(content any) (string, []byte, error) {
	var raw json.RawMessage
	switch c := content.(type) {
	case json.RawMessage:
		raw = c
	case []byte:
		raw = c
	default:
		b, err := json.Marshal(content)
		if err != nil {
			return "", nil, err
		}
		raw = b
	}
	canon, err := canonicalJSON(raw)
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(canon)
	return hex.EncodeToString(sum[:]), canon, nil
}

func entryHash(seq, orgID int64, action, contentHash, prevHash string) string {
	pre := fmt.Sprintf("%d\n%d\n%s\n%s\n%s\n", seq, orgID, action, contentHash, prevHash)
	sum := sha256.Sum256([]byte(pre))
	return hex.EncodeToString(sum[:])
}

// Append adds one entry to the organization's chain. It MUST run inside the
// caller's transaction; it takes the advisory lock, reads the previous tip,
// computes hashes and inserts the next sequence number. Concurrent callers
// block on the lock, so exactly one seq value is ever assigned.
func Append(
	ctx context.Context,
	tx pgx.Tx,
	q *sqlcgen.Queries,
	orgID, actorID int64,
	action string,
	content any,
) (int64, string, error) {
	if err := q.TakeAuditLock(ctx, sqlcgen.TakeAuditLockParams{
		Column1: advisoryNamespace,
		Column2: int32(orgID),
	}); err != nil {
		return 0, "", fmt.Errorf("audit lock: %w", err)
	}
	prevSeq, err := q.MaxAuditSeq(ctx, orgID)
	if err != nil {
		return 0, "", err
	}
	seq := prevSeq + 1

	var prevHash string
	if prevSeq > 0 {
		prev, err := q.GetAuditEntry(ctx, sqlcgen.GetAuditEntryParams{OrgID: orgID, Seq: prevSeq})
		if err != nil {
			return 0, "", fmt.Errorf("read chain tip: %w", err)
		}
		prevHash = prev.EntryHash
	}

	contentHash, _, err := CanonicalHash(content)
	if err != nil {
		return 0, "", err
	}
	hash := entryHash(seq, orgID, action, contentHash, prevHash)

	var actorArg *int64
	if actorID != 0 {
		actorArg = &actorID
	}
	if err := q.InsertAuditEntry(ctx, sqlcgen.InsertAuditEntryParams{
		OrgID:       orgID,
		Seq:         seq,
		ActorID:     actorArg,
		Action:      action,
		Content:     mustJSON(content),
		ContentHash: contentHash,
		PrevHash:    prevHash,
		EntryHash:   hash,
	}); err != nil {
		return 0, "", err
	}
	return seq, hash, nil
}

func mustJSON(content any) []byte {
	switch c := content.(type) {
	case json.RawMessage:
		return []byte(c)
	case []byte:
		return c
	default:
		b, err := json.Marshal(content)
		if err != nil {
			panic(err)
		}
		return b
	}
}

// Issue is one chain integrity problem.
type Issue struct {
	Seq    int64  `json:"seq,omitempty"`
	Kind   string `json:"kind"` // gap | tampered_content | broken_link | bad_hash
	Detail string `json:"detail"`
}

type Report struct {
	OrgID   int64   `json:"org_id"`
	Entries int64   `json:"entries"`
	MaxSeq  int64   `json:"max_seq"`
	Healthy bool    `json:"healthy"`
	Issues  []Issue `json:"issues"`
	TipHash string  `json:"tip_hash,omitempty"`
}

// Verify walks the whole chain of an organization and detects:
//   - gap:           a missing sequence number (deletion / missing entry);
//   - broken_link:   prev_hash does not equal the previous entry_hash
//     (rewrite or out-of-order reinsertion);
//   - tampered_content: stored content_hash disagrees with the canonical hash
//     of the stored content (in-place modification);
//   - bad_hash:      stored entry_hash disagrees with the recomputed value.
func Verify(ctx context.Context, q *sqlcgen.Queries, orgID int64) (Report, error) {
	rows, err := q.AllAuditEntries(ctx, orgID)
	if err != nil {
		return Report{}, err
	}
	rep := Report{OrgID: orgID, Healthy: true}
	var expectedPrev string
	var prevSeq int64

	for i, e := range rows {
		rep.MaxSeq = e.Seq
		if i == 0 {
			if e.Seq != 1 {
				rep.Healthy = false
				rep.Issues = append(rep.Issues, Issue{
					Seq: e.Seq, Kind: "gap",
					Detail: fmt.Sprintf("chain starts at seq %d, expected 1", e.Seq),
				})
			}
		} else if e.Seq != prevSeq+1 {
			rep.Healthy = false
			if e.Seq > prevSeq+1 {
				for missing := prevSeq + 1; missing < e.Seq; missing++ {
					rep.Issues = append(rep.Issues, Issue{
						Seq: missing, Kind: "gap",
						Detail: fmt.Sprintf("missing seq %d", missing),
					})
				}
			} else {
				rep.Issues = append(rep.Issues, Issue{
					Seq: e.Seq, Kind: "gap",
					Detail: fmt.Sprintf("duplicate/out-of-order seq %d after %d", e.Seq, prevSeq),
				})
			}
		}

		if e.PrevHash != expectedPrev {
			rep.Healthy = false
			rep.Issues = append(rep.Issues, Issue{
				Seq: e.Seq, Kind: "broken_link",
				Detail: fmt.Sprintf("prev_hash mismatch (link from %d)", prevSeq),
			})
		}

		// Re-hash the stored content. jsonb may have reordered keys;
		// canonicalJSON normalizes that before comparison.
		contentHash, _, err := CanonicalHash(json.RawMessage(e.Content))
		if err != nil {
			rep.Healthy = false
			rep.Issues = append(rep.Issues, Issue{
				Seq: e.Seq, Kind: "tampered_content", Detail: err.Error(),
			})
		} else if contentHash != e.ContentHash {
			rep.Healthy = false
			rep.Issues = append(rep.Issues, Issue{
				Seq: e.Seq, Kind: "tampered_content",
				Detail: "content hash does not match stored content",
			})
		}

		want := entryHash(e.Seq, e.OrgID, e.Action, e.ContentHash, e.PrevHash)
		if want != e.EntryHash {
			rep.Healthy = false
			rep.Issues = append(rep.Issues, Issue{
				Seq: e.Seq, Kind: "bad_hash",
				Detail: "entry hash does not match recomputed value",
			})
		}

		expectedPrev = e.EntryHash
		prevSeq = e.Seq
		rep.TipHash = e.EntryHash
	}
	rep.Entries = int64(len(rows))
	if len(rows) > 0 && rep.MaxSeq != rep.Entries {
		// Already itemized above, but keep the headline numbers honest.
		rep.Healthy = false
	}
	return rep, nil
}
