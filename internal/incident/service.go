// Package incident implements the SIRCC incident lifecycle business rules:
// linear phase transitions with optimistic versioning and idempotency,
// role/membership authorization, evidence capacity, and close gates.
package incident

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/db"
)

// MaxEvidence is the per-incident cap on text evidence entries.
const MaxEvidence = 50

// Error is a domain error carrying an HTTP status and stable code.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	HTTP    int    `json:"-"`
}

func (e *Error) Error() string { return e.Message }

func badRequest(code, msg string) *Error { return &Error{Code: code, Message: msg, HTTP: 400} }
func forbidden(msg string) *Error {
	return &Error{Code: "FORBIDDEN", Message: msg, HTTP: 403}
}
func notFound(msg string) *Error { return &Error{Code: "NOT_FOUND", Message: msg, HTTP: 404} }
func conflict(code, msg string) *Error { return &Error{Code: code, Message: msg, HTTP: 409} }
func internal(err error) *Error {
	return &Error{Code: "INTERNAL", Message: "internal error: " + err.Error(), HTTP: 500}
}

var phaseOrder = []db.IncidentStatus{
	db.IncidentStatusDetected,
	db.IncidentStatusTriaged,
	db.IncidentStatusContained,
	db.IncidentStatusEradicated,
	db.IncidentStatusRecovered,
	db.IncidentStatusPostmortem,
	db.IncidentStatusClosed,
}

// nextPhase returns the only phase reachable from s. There is exactly one
// legal successor per phase, so skipping is impossible by construction.
func nextPhase(s db.IncidentStatus) (db.IncidentStatus, bool) {
	for i, p := range phaseOrder {
		if p == s && i+1 < len(phaseOrder) {
			return phaseOrder[i+1], true
		}
	}
	return "", false
}

// Service holds the dependencies of the incident domain logic.
type Service struct {
	pool *pgxpool.Pool
	q    *db.Queries
}

func NewService(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, q: db.New(pool)}
}

// Durations are derived only from valid phase timestamps; a missing phase
// yields a nil duration rather than a fabricated end time.
type Durations struct {
	TimeToContainSeconds *float64 `json:"time_to_contain_seconds"`
	TimeToResolveSeconds *float64 `json:"time_to_resolve_seconds"`
}

// IncidentView is an incident plus its derived durations.
type IncidentView struct {
	Incident  db.Incident `json:"incident"`
	Durations Durations   `json:"durations"`
}

func computeDurations(phases []db.PhaseRecord) Durations {
	var detected, contained, recovered *time.Time
	for i := range phases {
		t := phases[i].EnteredAt
		switch phases[i].Phase {
		case db.IncidentStatusDetected:
			if detected == nil {
				detected = &t
			}
		case db.IncidentStatusContained:
			if contained == nil {
				contained = &t
			}
		case db.IncidentStatusRecovered:
			if recovered == nil {
				recovered = &t
			}
		}
	}
	var d Durations
	if detected != nil && contained != nil {
		s := contained.Sub(*detected).Seconds()
		d.TimeToContainSeconds = &s
	}
	if detected != nil && recovered != nil {
		s := recovered.Sub(*detected).Seconds()
		d.TimeToResolveSeconds = &s
	}
	return d
}

// --- authorization helpers -------------------------------------------------

func requireMember(ctx context.Context, q *db.Queries, incidentID uuid.UUID, userID string, role db.MemberRole) *Error {
	_, err := q.GetMember(ctx, db.GetMemberParams{IncidentID: incidentID, UserID: userID, Role: role})
	if errors.Is(err, pgx.ErrNoRows) {
		return forbidden(fmt.Sprintf("user %q is not an assigned %s on this incident", userID, role))
	}
	if err != nil {
		return internal(err)
	}
	return nil
}

func requireAnyMember(ctx context.Context, q *db.Queries, incidentID uuid.UUID, userID string) *Error {
	for _, role := range []db.MemberRole{db.MemberRoleAnalyst, db.MemberRoleResponder} {
		_, err := q.GetMember(ctx, db.GetMemberParams{IncidentID: incidentID, UserID: userID, Role: role})
		if err == nil {
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return internal(err)
		}
	}
	return forbidden(fmt.Sprintf("user %q is not assigned to this incident", userID))
}

// --- incident creation & reads ----------------------------------------------

func (s *Service) CreateIncident(ctx context.Context, actor, title, severity string) (*db.Incident, *Error) {
	title = strings.TrimSpace(title)
	if title == "" {
		return nil, badRequest("VALIDATION", "title is required")
	}
	sev := db.SeverityLevel(severity)
	if !sev.Valid() {
		return nil, badRequest("VALIDATION", "severity must be one of P1, P2, P3, P4")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	inc, err := q.CreateIncident(ctx, db.CreateIncidentParams{Title: title, Severity: sev, CreatedBy: actor})
	if err != nil {
		return nil, internal(err)
	}
	if _, err := q.OpenPhase(ctx, db.OpenPhaseParams{IncidentID: inc.ID, Phase: db.IncidentStatusDetected}); err != nil {
		return nil, internal(err)
	}
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: inc.ID, Actor: actor, Action: "incident_created",
		ToStatus: db.NullIncidentStatus{IncidentStatus: db.IncidentStatusDetected, Valid: true},
		Detail: []byte(`{}`),
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return &inc, nil
}

func (s *Service) GetIncident(ctx context.Context, id uuid.UUID) (*IncidentView, *Error) {
	inc, err := s.q.GetIncident(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("incident not found")
	}
	if err != nil {
		return nil, internal(err)
	}
	phases, err := s.q.ListPhases(ctx, id)
	if err != nil {
		return nil, internal(err)
	}
	return &IncidentView{Incident: inc, Durations: computeDurations(phases)}, nil
}

func (s *Service) ListIncidents(ctx context.Context) ([]db.Incident, *Error) {
	incs, err := s.q.ListIncidents(ctx)
	if err != nil {
		return nil, internal(err)
	}
	return incs, nil
}

func (s *Service) ListAudit(ctx context.Context, id uuid.UUID) ([]db.AuditEvent, *Error) {
	if _, err := s.GetIncident(ctx, id); err != nil {
		return nil, err
	}
	evs, err := s.q.ListAuditEvents(ctx, id)
	if err != nil {
		return nil, internal(err)
	}
	return evs, nil
}

// --- membership ---------------------------------------------------------------

func (s *Service) AssignMember(ctx context.Context, actor, actorRole string, incidentID uuid.UUID, userID, role string) *Error {
	if actorRole != "admin" {
		return forbidden("only admins can assign personnel")
	}
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return badRequest("VALIDATION", "user_id is required")
	}
	r := db.MemberRole(role)
	if !r.Valid() {
		return badRequest("VALIDATION", "role must be analyst or responder")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	inc, err := q.GetIncidentForUpdate(ctx, incidentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return notFound("incident not found")
	}
	if err != nil {
		return internal(err)
	}
	if inc.Status == db.IncidentStatusClosed {
		return badRequest("INCIDENT_CLOSED", "cannot assign personnel on a closed incident")
	}
	if err := q.AddMember(ctx, db.AddMemberParams{IncidentID: incidentID, UserID: userID, Role: r, AssignedBy: actor}); err != nil {
		return internal(err)
	}
	detail, _ := json.Marshal(map[string]string{"user_id": userID, "role": role})
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: incidentID, Actor: actor, Action: "member_assigned", Detail: detail,
	}); err != nil {
		return internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return internal(err)
	}
	return nil
}

func (s *Service) ListMembers(ctx context.Context, incidentID uuid.UUID) ([]db.IncidentMember, *Error) {
	ms, err := s.q.ListMembers(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	return ms, nil
}

// --- transitions ----------------------------------------------------------------

type TransitionInput struct {
	IncidentID      uuid.UUID
	To              string
	ExpectedVersion int32
	RequestID       string
	Actor           string
}

type TransitionResult struct {
	Incident db.Incident `json:"incident"`
	Replayed bool        `json:"replayed"`
}

func replayedResult(stored db.TransitionRequest) (*TransitionResult, *Error) {
	var res TransitionResult
	if err := json.Unmarshal(stored.Response, &res); err != nil {
		return nil, internal(err)
	}
	res.Replayed = true
	return &res, nil
}

// Transition advances an incident exactly one phase. The whole mutation —
// status bump, phase record close/open, audit event, idempotency record —
// commits in a single transaction, so a failed transition leaves no
// half-updated state.
func (s *Service) Transition(ctx context.Context, in TransitionInput) (*TransitionResult, *Error) {
	if strings.TrimSpace(in.RequestID) == "" {
		return nil, badRequest("VALIDATION", "request_id is required")
	}
	to := db.IncidentStatus(in.To)
	if !to.Valid() {
		return nil, badRequest("VALIDATION", "unknown target status")
	}

	// Fast path: this request_id already completed — return the original result.
	if prev, err := s.q.GetTransitionRequest(ctx, in.RequestID); err == nil {
		if prev.IncidentID != in.IncidentID {
			return nil, conflict("REQUEST_ID_CONFLICT", "request_id was already used for a different incident")
		}
		return replayedResult(prev)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	inc, err := q.GetIncidentForUpdate(ctx, in.IncidentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("incident not found")
	}
	if err != nil {
		return nil, internal(err)
	}

	// Re-check idempotency under the row lock: concurrent retries of the same
	// request_id serialize here and the loser replays the stored result.
	if prev, err := q.GetTransitionRequest(ctx, in.RequestID); err == nil {
		if prev.IncidentID != in.IncidentID {
			return nil, conflict("REQUEST_ID_CONFLICT", "request_id was already used for a different incident")
		}
		return replayedResult(prev)
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, internal(err)
	}

	if inc.Version != in.ExpectedVersion {
		return nil, conflict("VERSION_CONFLICT",
			fmt.Sprintf("stale version: expected %d, current version is %d", in.ExpectedVersion, inc.Version))
	}
	if inc.Status == to {
		return nil, badRequest("INVALID_TRANSITION", "incident is already in status "+string(to))
	}
	want, ok := nextPhase(inc.Status)
	if !ok || want != to {
		return nil, badRequest("INVALID_TRANSITION",
			fmt.Sprintf("cannot transition from %s to %s", inc.Status, to))
	}

	// Role + case-scope checks and phase gates.
	switch to {
	case db.IncidentStatusTriaged:
		if err := requireMember(ctx, q, inc.ID, in.Actor, db.MemberRoleAnalyst); err != nil {
			return nil, err
		}
		if inc.Severity == db.SeverityLevelP1 {
			n, err := q.CountResponders(ctx, inc.ID)
			if err != nil {
				return nil, internal(err)
			}
			if n == 0 {
				return nil, badRequest("GATE_UNMET", "P1 incident requires an assigned responder before triage can complete")
			}
		}
	case db.IncidentStatusContained, db.IncidentStatusEradicated,
		db.IncidentStatusRecovered, db.IncidentStatusPostmortem:
		if err := requireMember(ctx, q, inc.ID, in.Actor, db.MemberRoleResponder); err != nil {
			return nil, err
		}
	case db.IncidentStatusClosed:
		if err := requireMember(ctx, q, inc.ID, in.Actor, db.MemberRoleResponder); err != nil {
			return nil, err
		}
		if strings.TrimSpace(inc.RootCause) == "" {
			return nil, badRequest("GATE_UNMET", "root cause is required before closing")
		}
		if strings.TrimSpace(inc.LessonsLearned) == "" {
			return nil, badRequest("GATE_UNMET", "lessons learned are required before closing")
		}
		n, err := q.CountActionItems(ctx, inc.ID)
		if err != nil {
			return nil, internal(err)
		}
		if n == 0 {
			return nil, badRequest("GATE_UNMET", "at least one action item with an owner and due date is required before closing")
		}
	}

	updated, err := q.AdvanceIncident(ctx, db.AdvanceIncidentParams{
		ID: inc.ID, Status: to, ExpectedVersion: inc.Version,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, conflict("VERSION_CONFLICT", "incident version changed concurrently")
	}
	if err != nil {
		return nil, internal(err)
	}
	if err := q.CloseOpenPhase(ctx, inc.ID); err != nil {
		return nil, internal(err)
	}
	if _, err := q.OpenPhase(ctx, db.OpenPhaseParams{IncidentID: inc.ID, Phase: to}); err != nil {
		return nil, internal(err)
	}
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: inc.ID, Actor: in.Actor, Action: "transition",
		FromStatus: db.NullIncidentStatus{IncidentStatus: inc.Status, Valid: true},
		ToStatus:   db.NullIncidentStatus{IncidentStatus: to, Valid: true},
		Detail:     []byte(`{}`),
		RequestID:  &in.RequestID,
	}); err != nil {
		return nil, internal(err)
	}

	res := &TransitionResult{Incident: updated}
	payload, err := json.Marshal(res)
	if err != nil {
		return nil, internal(err)
	}
	if err := q.InsertTransitionRequest(ctx, db.InsertTransitionRequestParams{
		RequestID: in.RequestID, IncidentID: inc.ID, Response: payload,
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return res, nil
}

// --- postmortem -----------------------------------------------------------------

func (s *Service) SetPostmortem(ctx context.Context, actor string, incidentID uuid.UUID, rootCause, lessons string) (*db.Incident, *Error) {
	if strings.TrimSpace(rootCause) == "" || strings.TrimSpace(lessons) == "" {
		return nil, badRequest("VALIDATION", "root_cause and lessons_learned are both required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	inc, err := q.GetIncidentForUpdate(ctx, incidentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("incident not found")
	}
	if err != nil {
		return nil, internal(err)
	}
	if err := requireMember(ctx, q, inc.ID, actor, db.MemberRoleResponder); err != nil {
		return nil, err
	}
	if inc.Status != db.IncidentStatusPostmortem {
		return nil, badRequest("INVALID_PHASE", "postmortem fields can only be set during the postmortem phase")
	}
	updated, err := q.SetPostmortem(ctx, db.SetPostmortemParams{
		ID: inc.ID, RootCause: rootCause, LessonsLearned: lessons,
	})
	if err != nil {
		return nil, internal(err)
	}
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: inc.ID, Actor: actor, Action: "postmortem_recorded", Detail: []byte(`{}`),
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return &updated, nil
}

// --- evidence ---------------------------------------------------------------------

func (s *Service) AddEvidence(ctx context.Context, actor string, incidentID uuid.UUID, content string) (*db.Evidence, *Error) {
	if strings.TrimSpace(content) == "" {
		return nil, badRequest("VALIDATION", "content is required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	inc, err := q.GetIncidentForUpdate(ctx, incidentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("incident not found")
	}
	if err != nil {
		return nil, internal(err)
	}
	if inc.Status == db.IncidentStatusClosed {
		return nil, badRequest("INCIDENT_CLOSED", "cannot add evidence to a closed incident")
	}
	if err := requireMember(ctx, q, inc.ID, actor, db.MemberRoleAnalyst); err != nil {
		return nil, err
	}
	// Atomic conditional increment: concurrent adds serialize on the row lock
	// held by this UPDATE, so the 50-entry cap cannot be bypassed.
	seq, err := q.TryIncrementEvidenceCount(ctx, inc.ID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, conflict("EVIDENCE_LIMIT", fmt.Sprintf("evidence limit of %d entries reached", MaxEvidence))
	}
	if err != nil {
		return nil, internal(err)
	}
	ev, err := q.InsertEvidence(ctx, db.InsertEvidenceParams{
		IncidentID: inc.ID, Seq: seq, Content: content, SubmittedBy: actor,
	})
	if err != nil {
		return nil, internal(err)
	}
	detail, _ := json.Marshal(map[string]any{"evidence_id": ev.ID, "seq": ev.Seq})
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: inc.ID, Actor: actor, Action: "evidence_added", Detail: detail,
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return &ev, nil
}

// AddEvidenceNote is the only way to correct submitted evidence: an
// append-only note linked to the original, immutable entry.
func (s *Service) AddEvidenceNote(ctx context.Context, actor string, evidenceID uuid.UUID, note string) (*db.EvidenceNote, *Error) {
	if strings.TrimSpace(note) == "" {
		return nil, badRequest("VALIDATION", "note is required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	ev, err := q.GetEvidence(ctx, evidenceID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("evidence not found")
	}
	if err != nil {
		return nil, internal(err)
	}
	if err := requireMember(ctx, q, ev.IncidentID, actor, db.MemberRoleAnalyst); err != nil {
		return nil, err
	}
	n, err := q.AddEvidenceNote(ctx, db.AddEvidenceNoteParams{
		EvidenceID: evidenceID, Note: note, CreatedBy: actor,
	})
	if err != nil {
		return nil, internal(err)
	}
	detail, _ := json.Marshal(map[string]any{"evidence_id": evidenceID, "note_id": n.ID})
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: ev.IncidentID, Actor: actor, Action: "evidence_note_added", Detail: detail,
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return &n, nil
}

// --- action items -------------------------------------------------------------------

func (s *Service) CreateActionItem(ctx context.Context, actor string, incidentID uuid.UUID, title, ownerID string, dueAt time.Time) (*db.ActionItem, *Error) {
	if strings.TrimSpace(title) == "" || strings.TrimSpace(ownerID) == "" {
		return nil, badRequest("VALIDATION", "title and owner_id are required")
	}
	if dueAt.IsZero() {
		return nil, badRequest("VALIDATION", "due_at is required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	inc, err := q.GetIncidentForUpdate(ctx, incidentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("incident not found")
	}
	if err != nil {
		return nil, internal(err)
	}
	if inc.Status == db.IncidentStatusClosed {
		return nil, badRequest("INCIDENT_CLOSED", "cannot add action items to a closed incident")
	}
	if err := requireAnyMember(ctx, q, inc.ID, actor); err != nil {
		return nil, err
	}
	item, err := q.CreateActionItem(ctx, db.CreateActionItemParams{
		IncidentID: inc.ID, Title: title, OwnerID: ownerID, DueAt: dueAt,
	})
	if err != nil {
		return nil, internal(err)
	}
	detail, _ := json.Marshal(map[string]any{"action_item_id": item.ID, "owner_id": ownerID, "due_at": dueAt})
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: inc.ID, Actor: actor, Action: "action_item_created", Detail: detail,
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return &item, nil
}

// RescheduleActionItem moves the due date and bumps due_version, which
// invalidates any not-yet-sent reminder scheduled under the old version.
func (s *Service) RescheduleActionItem(ctx context.Context, actor string, itemID uuid.UUID, newDue time.Time) (*db.ActionItem, *Error) {
	if newDue.IsZero() {
		return nil, badRequest("VALIDATION", "due_at is required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, internal(err)
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	item, err := q.GetActionItemForUpdate(ctx, itemID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("action item not found")
	}
	if err != nil {
		return nil, internal(err)
	}
	if err := requireAnyMember(ctx, q, item.IncidentID, actor); err != nil {
		return nil, err
	}
	updated, err := q.RescheduleActionItem(ctx, db.RescheduleActionItemParams{ID: itemID, DueAt: newDue})
	if err != nil {
		return nil, internal(err)
	}
	detail, _ := json.Marshal(map[string]any{
		"action_item_id": itemID, "old_due_at": item.DueAt, "new_due_at": newDue,
		"due_version": updated.DueVersion,
	})
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: item.IncidentID, Actor: actor, Action: "action_item_rescheduled", Detail: detail,
	}); err != nil {
		return nil, internal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, internal(err)
	}
	return &updated, nil
}

func (s *Service) ListActionItems(ctx context.Context, incidentID uuid.UUID) ([]db.ActionItem, *Error) {
	items, err := s.q.ListActionItems(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	return items, nil
}

func (s *Service) ListReminders(ctx context.Context, incidentID uuid.UUID) ([]db.Reminder, *Error) {
	rs, err := s.q.ListRemindersForIncident(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	return rs, nil
}

// --- export ---------------------------------------------------------------------------

type EvidenceSummaryItem struct {
	ID          uuid.UUID         `json:"id"`
	Seq         int32             `json:"seq"`
	Excerpt     string            `json:"excerpt"`
	SubmittedBy string            `json:"submitted_by"`
	SubmittedAt time.Time         `json:"submitted_at"`
	Notes       []db.EvidenceNote `json:"notes"`
}

type EvidenceSummary struct {
	Count int                  `json:"count"`
	Items []EvidenceSummaryItem `json:"items"`
}

type Export struct {
	Incident    db.Incident     `json:"incident"`
	Durations   Durations       `json:"durations"`
	Phases      []db.PhaseRecord `json:"phases"`
	Evidence    EvidenceSummary `json:"evidence"`
	ActionItems []db.ActionItem `json:"action_items"`
	AuditEvents []db.AuditEvent `json:"audit_events"`
}

func (s *Service) Export(ctx context.Context, incidentID uuid.UUID) (*Export, *Error) {
	view, svcErr := s.GetIncident(ctx, incidentID)
	if svcErr != nil {
		return nil, svcErr
	}
	phases, err := s.q.ListPhases(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	evs, err := s.q.ListEvidence(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	notes, err := s.q.ListNotesForIncident(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	items, err := s.q.ListActionItems(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}
	audits, err := s.q.ListAuditEvents(ctx, incidentID)
	if err != nil {
		return nil, internal(err)
	}

	notesByEvidence := map[uuid.UUID][]db.EvidenceNote{}
	for _, n := range notes {
		notesByEvidence[n.EvidenceID] = append(notesByEvidence[n.EvidenceID], n)
	}
	summary := EvidenceSummary{Count: len(evs), Items: make([]EvidenceSummaryItem, 0, len(evs))}
	for _, ev := range evs {
		excerpt := ev.Content
		if len(excerpt) > 200 {
			excerpt = excerpt[:200] + "…"
		}
		summary.Items = append(summary.Items, EvidenceSummaryItem{
			ID: ev.ID, Seq: ev.Seq, Excerpt: excerpt,
			SubmittedBy: ev.SubmittedBy, SubmittedAt: ev.SubmittedAt,
			Notes: notesByEvidence[ev.ID],
		})
	}
	return &Export{
		Incident: view.Incident, Durations: view.Durations, Phases: phases,
		Evidence: summary, ActionItems: items, AuditEvents: audits,
	}, nil
}
