package service

import (
	"context"

	"github.com/google/uuid"

	"sircc/internal/store"
)

// attachFull fills the collections shown on a detailed case read.
func (s *Service) attachFull(ctx context.Context, q *store.Queries, incidentID uuid.UUID, d *IncidentDetail) error {
	evRows, err := q.ListEvidenceWithAuthor(ctx, incidentID)
	if err != nil {
		return err
	}
	notes, err := q.ListNotesByIncidentWithAuthor(ctx, incidentID)
	if err != nil {
		return err
	}
	notesByEvidence := map[uuid.UUID][]EvidenceNoteView{}
	for _, n := range notes {
		notesByEvidence[n.EvidenceID] = append(notesByEvidence[n.EvidenceID], EvidenceNoteView{
			ID:         n.ID.String(),
			EvidenceID: n.EvidenceID.String(),
			AuthorID:   n.AuthorID.String(),
			AuthorName: n.AuthorFullName,
			Note:       n.Note,
			CreatedAt:  ts(n.CreatedAt),
		})
	}
	d.Evidence = make([]EvidenceView, 0, len(evRows))
	for _, e := range evRows {
		v := evidenceView(evidenceFromListRow(e), e.AuthorFullName)
		v.Notes = notesByEvidence[e.ID]
		d.Evidence = append(d.Evidence, v)
	}

	items, err := q.ListActionItemsByIncident(ctx, incidentID)
	if err != nil {
		return err
	}
	d.ActionItems = make([]ActionItemView, 0, len(items))
	for _, it := range items {
		d.ActionItems = append(d.ActionItems, actionItemRowView(it))
	}

	reminders, err := q.ListRemindersByIncident(ctx, incidentID)
	if err != nil {
		return err
	}
	d.Reminders = make([]ReminderView, 0, len(reminders))
	for _, r := range reminders {
		d.Reminders = append(d.Reminders, reminderView(r))
	}

	audits, err := q.ListAuditByIncident(ctx, incidentID)
	if err != nil {
		return err
	}
	d.Audit = make([]AuditView, 0, len(audits))
	for _, a := range audits {
		d.Audit = append(d.Audit, auditView(a))
	}
	return nil
}

func auditView(a store.AuditEvent) AuditView {
	return AuditView{
		ID:         a.ID.String(),
		Action:     a.Action,
		ActorID:    a.ActorID.String(),
		FromStatus: a.FromStatus,
		ToStatus:   a.ToStatus,
		RequestID:  uuidFromPg(a.RequestID),
		Detail:     jsonDetail(a.Detail),
		CreatedAt:  ts(a.CreatedAt),
	}
}
