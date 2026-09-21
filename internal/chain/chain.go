// Package chain implements the per-case append-only hash chain.
package chain

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"forensiccore/internal/domain"
	"forensiccore/internal/hashing"
	"forensiccore/internal/idutil"

	"gorm.io/gorm"
)

// Sentinel errors.
var (
	ErrCaseNotFound  = errors.New("case not found")
	ErrChainConflict = errors.New("chain conflict: concurrent append, retry")
	ErrNotFound      = errors.New("event not found")
)

// Issue codes returned by Verify.
const (
	IssueMissingEvent  = "missing_event"
	IssueGap           = "gap_in_sequence"
	IssueBrokenLink    = "broken_prev_link"
	IssueContentDigest = "content_digest_mismatch"
	IssueEventOrder    = "unexpected_event_at_seq"
	IssueNoEvents      = "no_events"
)

// Issue is one detected integrity problem.
type Issue struct {
	Code   string `json:"code"`
	Seq    int64  `json:"seq,omitempty"`
	Detail string `json:"detail"`
}

// VerifyReport is the result of walking a case chain.
type VerifyReport struct {
	CaseID      string    `json:"case_id"`
	GenesisHash string    `json:"genesis_hash"`
	HeadSeq     int64     `json:"head_seq"`
	HeadDigest  string    `json:"head_digest"`
	Intact      bool      `json:"intact"`
	Issues      []Issue   `json:"issues"`
	EventCount  int       `json:"event_count"`
	VerifiedAt  time.Time `json:"verified_at"`
}

// Appender is the hash-chain writer and reader for one database.
type Appender struct {
	db *gorm.DB

	// caseLocks serializes appends for a given case inside this process. The
	// (case_id, seq) unique index remains the cross-process fork barrier.
	caseLocks sync.Map // caseID -> *sync.Mutex
}

// New returns an Appender.
func New(db *gorm.DB) *Appender {
	return &Appender{db: db}
}

// AppendInput is one append request.
type AppendInput struct {
	CaseID    string
	EventType string
	Actor     string
	Payload   any
	// Occurred defaults to time.Now() in UTC when zero.
	Occurred time.Time
}

const maxAppendAttempts = 12

// Append atomically links a new event to the case head.
//
// It re-reads the head inside a DB transaction, computes the next link and
// inserts with the unique (case_id, seq) constraint. A lost race surfaces as a
// duplicate-key error and is retried (bounded) so concurrent appends never
// fork and never silently drop an event.
func (a *Appender) Append(ctx context.Context, in AppendInput) (*domain.ChainEvent, error) {
	if in.Occurred.IsZero() {
		in.Occurred = time.Now().UTC()
	}
	lock := a.lockFor(in.CaseID)
	lock.Lock()
	defer lock.Unlock()

	var lastErr error
	for attempt := 0; attempt < maxAppendAttempts; attempt++ {
		var ev *domain.ChainEvent
		err := a.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
			var inner error
			ev, inner = a.attemptAppend(ctx, tx, in)
			return inner
		})
		if err == nil {
			return ev, nil
		}
		if IsDuplicateKeyErr(err) {
			lastErr = err
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(attempt+1) * 5 * time.Millisecond):
			}
			continue
		}
		return nil, err
	}
	return nil, fmt.Errorf("%w: %v", ErrChainConflict, lastErr)
}

// AppendOn works like Append but participates in an existing GORM transaction:
// pass the tx handle so the chain insert commits or rolls back together with
// the caller's other writes. It must be called inside the caller's
// transaction; use Append for a standalone insert.
//
// AppendOn performs exactly one insert: the per-case in-process lock prevents
// same-process seq races, and a cross-process unique-constraint loss must
// abort the surrounding transaction (poisoned in MySQL/InnoDB) rather than be
// retried inside it.
func (a *Appender) AppendOn(ctx context.Context, tx *gorm.DB, in AppendInput) (*domain.ChainEvent, error) {
	if in.Occurred.IsZero() {
		in.Occurred = time.Now().UTC()
	}
	lock := a.lockFor(in.CaseID)
	lock.Lock()
	defer lock.Unlock()
	return a.attemptAppend(ctx, tx, in)
}

// attemptAppend performs one try. db must already be inside a transaction when
// the caller needs the insert to be atomic with other writes.
func (a *Appender) attemptAppend(ctx context.Context, db *gorm.DB, in AppendInput) (*domain.ChainEvent, error) {
	contentJSON, err := hashing.CanonicalJSON(in.CaseID, in.EventType, in.Actor, in.Occurred, in.Payload)
	if err != nil {
		return nil, err
	}

	var kase domain.Case
	if err := db.WithContext(ctx).First(&kase, "id = ?", in.CaseID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrCaseNotFound
		}
		return nil, err
	}

	var head domain.ChainEvent
	headErr := db.WithContext(ctx).Where("case_id = ?", in.CaseID).Order("seq DESC").First(&head).Error
	if headErr != nil && !errors.Is(headErr, gorm.ErrRecordNotFound) {
		return nil, headErr
	}

	nextSeq := int64(1)
	prevDigest := kase.GenesisHash
	if headErr == nil {
		nextSeq = head.Seq + 1
		prevDigest = head.Digest
	}

	ev := &domain.ChainEvent{
		ID:          idutil.New(),
		CaseID:      in.CaseID,
		Seq:         nextSeq,
		EventType:   in.EventType,
		Actor:       in.Actor,
		PrevDigest:  prevDigest,
		ContentJSON: contentJSON,
		Digest:      hashing.LinkDigest(prevDigest, contentJSON),
		CreatedAt:   in.Occurred,
	}
	if err := db.WithContext(ctx).Create(ev).Error; err != nil {
		return nil, err
	}
	return ev, nil
}

func (a *Appender) lockFor(caseID string) *sync.Mutex {
	m, _ := a.caseLocks.LoadOrStore(caseID, &sync.Mutex{})
	return m.(*sync.Mutex)
}

// ListEvents returns all events of a case ordered by seq.
func (a *Appender) ListEvents(ctx context.Context, caseID string) ([]domain.ChainEvent, error) {
	var evs []domain.ChainEvent
	err := a.db.WithContext(ctx).Where("case_id = ?", caseID).Order("seq ASC").Find(&evs).Error
	return evs, err
}

// GetEvent fetches a single event.
func (a *Appender) GetEvent(ctx context.Context, id string) (*domain.ChainEvent, error) {
	var ev domain.ChainEvent
	if err := a.db.WithContext(ctx).First(&ev, "id = ?", id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &ev, nil
}

// Verify walks the chain from the genesis hash and reports every integrity
// problem: missing/gapped sequences, broken previous links, tampered content
// (digest mismatch) and ordering inconsistencies.
func (a *Appender) Verify(ctx context.Context, caseID string) (*VerifyReport, error) {
	report := &VerifyReport{CaseID: caseID, VerifiedAt: time.Now().UTC(), Issues: []Issue{}}

	var kase domain.Case
	if err := a.db.WithContext(ctx).First(&kase, "id = ?", caseID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrCaseNotFound
		}
		return nil, err
	}
	report.GenesisHash = kase.GenesisHash

	evs, err := a.ListEvents(ctx, caseID)
	if err != nil {
		return nil, err
	}
	report.EventCount = len(evs)
	if len(evs) == 0 {
		report.Intact = false
		report.Issues = append(report.Issues, Issue{Code: IssueNoEvents, Detail: "case has no chain events"})
		return report, nil
	}

	// Sort defensively; storage already guarantees seq uniqueness.
	sort.Slice(evs, func(i, j int) bool { return evs[i].Seq < evs[j].Seq })

	expectedPrev := kase.GenesisHash
	var expectedSeq int64 = 1
	for _, ev := range evs {
		if ev.Seq != expectedSeq {
			if ev.Seq > expectedSeq {
				report.Issues = append(report.Issues, Issue{
					Code: IssueGap, Seq: ev.Seq,
					Detail: fmt.Sprintf("expected seq %d next, found %d (events missing or deleted)", expectedSeq, ev.Seq),
				})
			} else {
				report.Issues = append(report.Issues, Issue{
					Code: IssueEventOrder, Seq: ev.Seq,
					Detail: fmt.Sprintf("out-of-order/duplicate seq %d where %d expected", ev.Seq, expectedSeq),
				})
			}
		}
		if ev.PrevDigest != expectedPrev {
			report.Issues = append(report.Issues, Issue{
				Code: IssueBrokenLink, Seq: ev.Seq,
				Detail: "prev_digest does not match the previous event digest; chain reordered or an event replaced",
			})
		}
		wantDigest := hashing.LinkDigest(ev.PrevDigest, ev.ContentJSON)
		if ev.Digest != wantDigest {
			report.Issues = append(report.Issues, Issue{
				Code: IssueContentDigest, Seq: ev.Seq,
				Detail: "stored digest does not match SHA-256(prev_digest || content); content or digest tampered",
			})
		}
		expectedPrev = ev.Digest
		expectedSeq = ev.Seq + 1
	}

	head := evs[len(evs)-1]
	report.HeadSeq = head.Seq
	report.HeadDigest = head.Digest
	report.Intact = len(report.Issues) == 0
	return report, nil
}

// IsDuplicateKeyErr reports whether err is a unique-constraint violation.
func IsDuplicateKeyErr(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	return strings.Contains(s, "Duplicate entry") || // mysql
		strings.Contains(s, "UNIQUE constraint failed") || // sqlite
		strings.Contains(s, "duplicate key") // postgres-ish
}
