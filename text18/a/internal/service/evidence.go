package service

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/domain"
	"sircc/internal/store"
)

type EvidenceView struct {
	ID          string             `json:"id"`
	Seq         int32              `json:"seq"`
	Content     string             `json:"content"`
	SubmittedBy string             `json:"submitted_by"`
	AuthorName  string             `json:"author_full_name,omitempty"`
	RequestID   *string            `json:"request_id,omitempty"`
	CreatedAt   time.Time          `json:"created_at"`
	Notes       []EvidenceNoteView `json:"notes,omitempty"`
}

type EvidenceNoteView struct {
	ID         string    `json:"id"`
	EvidenceID string    `json:"evidence_id"`
	AuthorID   string    `json:"author_id"`
	AuthorName string    `json:"author_full_name,omitempty"`
	Note       string    `json:"note"`
	CreatedAt  time.Time `json:"created_at"`
}

type AddEvidenceInput struct {
	Content   string
	RequestID uuid.UUID
}

// AddEvidence appends one text evidence item. The incident row is locked
// FOR UPDATE for the whole transaction, serializing concurrent inserts:
// at most 50 items can ever be created, never 51 via a race.
func (s *Service) AddEvidence(ctx context.Context, caller *store.User,
	incidentID uuid.UUID, in AddEvidenceInput,
) (Result, error) {
	content := strings.TrimSpace(in.Content)
	if content == "" || len(in.Content) > 100000 {
		return Result{}, domain.ErrValidation
	}
	incident := incidentID

	return s.runMutated(ctx, caller.ID, in.RequestID, &incident, "POST",
		"/api/v1/incidents/"+incidentID.String()+"/evidence",
		func(ctx context.Context, q *store.Queries) (any, int, error) {
			inc, err := q.GetIncidentForUpdate(ctx, incidentID)
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if inc.Status == domain.StatusClosed {
				return nil, 0, domain.ErrInvalidTransition
			}
			if caller.Role != domain.RoleAdmin {
				if _, err := s.requireCaseAccess(ctx, q, incidentID, caller, domain.RoleAnalyst); err != nil {
					return nil, 0, err
				}
			}

			maxSeq, err := q.MaxEvidenceSeq(ctx, incidentID)
			if err != nil {
				return nil, 0, err
			}
			if maxSeq >= int32(domain.MaxEvidencePerIncident) {
				return nil, 0, domain.ErrEvidenceCap
			}
			ev, err := q.CreateEvidence(ctx, store.CreateEvidenceParams{
				IncidentID:  incidentID,
				Seq:         maxSeq + 1,
				Content:     in.Content,
				SubmittedBy: caller.ID,
				RequestID:   pgUUID(in.RequestID),
			})
			if err != nil {
				return nil, 0, err
			}
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: incidentID,
				ActorID:    caller.ID,
				Action:     "evidence.created",
				FromStatus: strPtr(inc.Status),
				ToStatus:   strPtr(inc.Status),
				RequestID:  pgUUID(in.RequestID),
				Detail:     []byte(`{"evidence_id":"` + ev.ID.String() + `","seq":` + itoa(int64(ev.Seq)) + `}`),
			}); err != nil {
				return nil, 0, err
			}
			return evidenceView(ev, ""), http.StatusCreated, nil
		})
}

// AddEvidenceNote appends a correction note to existing evidence. The
// original content can never be overwritten; a correction is always a new
// linked note.
func (s *Service) AddEvidenceNote(ctx context.Context, caller *store.User,
	incidentID, evidenceID uuid.UUID, note string,
) (EvidenceNoteView, error) {
	note = strings.TrimSpace(note)
	if note == "" || len(note) > 100000 {
		return EvidenceNoteView{}, domain.ErrValidation
	}
	var created store.EvidenceNote
	err := s.inTx(ctx, func(q *store.Queries) error {
		ev, err := q.GetEvidence(ctx, evidenceID)
		if err != nil {
			if err == pgx.ErrNoRows {
				return domain.ErrNotFound
			}
			return err
		}
		if ev.IncidentID != incidentID {
			return domain.ErrNotFound
		}
		inc, err := q.GetIncidentForUpdate(ctx, incidentID)
		if err != nil {
			return err
		}
		if inc.Status == domain.StatusClosed {
			return domain.ErrInvalidTransition
		}
		if caller.Role != domain.RoleAdmin {
			if _, err := s.requireCaseAccess(ctx, q, incidentID, caller, domain.RoleAnalyst); err != nil {
				return err
			}
		}
		created, err = q.CreateEvidenceNote(ctx, store.CreateEvidenceNoteParams{
			EvidenceID: evidenceID,
			IncidentID: incidentID,
			AuthorID:   caller.ID,
			Note:       note,
		})
		if err != nil {
			return err
		}
		_, err = q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
			IncidentID: incidentID,
			ActorID:    caller.ID,
			Action:     "evidence.note.created",
			FromStatus: strPtr(inc.Status),
			ToStatus:   strPtr(inc.Status),
			Detail:     []byte(`{"evidence_id":"` + evidenceID.String() + `","note_id":"` + created.ID.String() + `"}`),
		})
		return err
	})
	if err != nil {
		return EvidenceNoteView{}, err
	}
	return EvidenceNoteView{
		ID:         created.ID.String(),
		EvidenceID: created.EvidenceID.String(),
		AuthorID:   created.AuthorID.String(),
		Note:       created.Note,
		CreatedAt:  ts(created.CreatedAt),
	}, nil
}

// ListEvidence returns all evidence with correction notes for a case.
func (s *Service) ListEvidence(ctx context.Context, caller *store.User, incidentID uuid.UUID) ([]EvidenceView, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	q := store.New(tx)
	if _, err := q.GetIncident(ctx, incidentID); err != nil {
		if err == pgx.ErrNoRows {
			return nil, domain.ErrNotFound
		}
		return nil, err
	}
	if _, err := s.requireCaseAccess(ctx, q, incidentID, caller); err != nil {
		return nil, err
	}
	rows, err := q.ListEvidenceWithAuthor(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	notes, err := q.ListNotesByIncidentWithAuthor(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	byEvidence := map[uuid.UUID][]EvidenceNoteView{}
	for _, n := range notes {
		byEvidence[n.EvidenceID] = append(byEvidence[n.EvidenceID], EvidenceNoteView{
			ID:         n.ID.String(),
			EvidenceID: n.EvidenceID.String(),
			AuthorID:   n.AuthorID.String(),
			AuthorName: n.AuthorFullName,
			Note:       n.Note,
			CreatedAt:  ts(n.CreatedAt),
		})
	}
	out := make([]EvidenceView, 0, len(rows))
	for _, e := range rows {
		v := evidenceView(evidenceFromListRow(e), e.AuthorFullName)
		v.Notes = byEvidence[e.ID]
		out = append(out, v)
	}
	return out, tx.Commit(ctx)
}

func evidenceView(e store.Evidence, authorName string) EvidenceView {
	return EvidenceView{
		ID:          e.ID.String(),
		Seq:         e.Seq,
		Content:     e.Content,
		SubmittedBy: e.SubmittedBy.String(),
		AuthorName:  authorName,
		RequestID:   uuidFromPg(e.RequestID),
		CreatedAt:   ts(e.CreatedAt),
	}
}

func evidenceFromListRow(r store.ListEvidenceWithAuthorRow) store.Evidence {
	return store.Evidence{
		ID:          r.ID,
		IncidentID:  r.IncidentID,
		Seq:         r.Seq,
		Content:     r.Content,
		SubmittedBy: r.SubmittedBy,
		RequestID:   r.RequestID,
		CreatedAt:   r.CreatedAt,
	}
}
