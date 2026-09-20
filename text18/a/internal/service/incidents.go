package service

import (
	"context"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/domain"
	"sircc/internal/store"
)

// PhaseMetrics are derived exclusively from incident_phases rows. An
// unfinished phase contributes no end timestamp, so durations that need a
// later phase stay null rather than being faked with now().
type PhaseMetrics struct {
	TimeToContainmentSeconds *float64           `json:"time_to_containment_seconds"`
	ContainmentPhaseSeconds  *float64           `json:"containment_phase_seconds"`
	TimeToResolutionSeconds  *float64           `json:"time_to_resolution_seconds"`
	TimeToClosureSeconds     *float64           `json:"time_to_closure_seconds"`
	PhaseDurationsSeconds    map[string]float64 `json:"phase_durations_seconds"`
}

type CreateIncidentInput struct {
	Title       string
	Description string
	Severity    string
	RequestID   uuid.UUID
}

func (s *Service) CreateIncident(ctx context.Context, caller *store.User, in CreateIncidentInput) (Result, error) {
	in.Title = strings.TrimSpace(in.Title)
	if in.Title == "" || len(in.Title) > 500 {
		return Result{}, domain.ErrValidation
	}
	switch in.Severity {
	case "P1", "P2", "P3", "P4":
	default:
		return Result{}, domain.ErrValidation
	}
	incidentID := uuid.New()
	now := s.clock.Now()

	return s.runMutated(ctx, caller.ID, in.RequestID, &incidentID, "POST",
		"/api/v1/incidents", func(ctx context.Context, q *store.Queries) (any, int, error) {
			inc, err := q.CreateIncidentWithID(ctx, store.CreateIncidentWithIDParams{
				ID:          incidentID,
				Title:       in.Title,
				Description: in.Description,
				Severity:    in.Severity,
				CreatedBy:   caller.ID,
				DetectedAt:  pgTimestamp(now),
			})
			if err != nil {
				return nil, 0, err
			}
			// The creator is a case member so they retain scoped access.
			if caller.Role != domain.RoleAdmin {
				if err := q.AddMember(ctx, store.AddMemberParams{
					IncidentID: inc.ID,
					UserID:     caller.ID,
					CaseRole:   caller.Role,
					AssignedBy: pgUUID(caller.ID),
				}); err != nil {
					return nil, 0, err
				}
			}
			if _, err := q.CreatePhase(ctx, store.CreatePhaseParams{
				IncidentID: inc.ID,
				Phase:      domain.PhaseDetection,
				ActorID:    caller.ID,
				RequestID:  pgUUID(in.RequestID),
				EnteredAt:  pgTimestamp(now),
			}); err != nil {
				return nil, 0, err
			}
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: inc.ID,
				ActorID:    caller.ID,
				Action:     "incident.created",
				ToStatus:   strPtr(domain.StatusDetected),
				RequestID:  pgUUID(in.RequestID),
				Detail:     []byte(`{"severity":"` + in.Severity + `"}`),
			}); err != nil {
				return nil, 0, err
			}
			detail, err := s.incidentDetail(ctx, q, inc, false)
			if err != nil {
				return nil, 0, err
			}
			return detail, http.StatusCreated, nil
		})
}

func strPtr(v string) *string { return &v }

type IncidentDetail struct {
	IncidentView
	Evidence    []EvidenceView     `json:"evidence,omitempty"`
	Notes       []EvidenceNoteView `json:"evidence_notes,omitempty"`
	ActionItems []ActionItemView   `json:"action_items,omitempty"`
	Reminders   []ReminderView     `json:"reminders,omitempty"`
	Audit       []AuditView        `json:"audit,omitempty"`
}

func (s *Service) incidentDetail(ctx context.Context, q *store.Queries,
	inc store.Incident, full bool,
) (*IncidentDetail, error) {
	d := &IncidentDetail{IncidentView: incidentView(inc)}

	phases, err := q.ListPhasesWithActor(ctx, inc.ID)
	if err != nil {
		return nil, err
	}
	for _, p := range phases {
		rid := uuidFromPg(p.RequestID)
		d.Phases = append(d.Phases, PhaseView{
			ID:            p.ID.String(),
			Phase:         p.Phase,
			ActorID:       p.ActorID.String(),
			ActorUsername: p.ActorUsername,
			RequestID:     rid,
			EnteredAt:     ts(p.EnteredAt),
		})
	}
	members, err := q.ListMembers(ctx, inc.ID)
	if err != nil {
		return nil, err
	}
	for _, m := range members {
		mv := MemberView{
			UserID:    m.UserID.String(),
			Username:  m.Username,
			FullName:  m.FullName,
			CaseRole:  m.CaseRole,
			CreatedAt: ts(m.CreatedAt),
		}
		if m.AssignedBy.Valid {
			mv.AssignedBy = strPtr(uuid.UUID(m.AssignedBy.Bytes).String())
		}
		d.Members = append(d.Members, mv)
	}
	d.Metrics = computeMetrics(phasesToTimes(phases))

	if full {
		if err := s.attachFull(ctx, q, inc.ID, d); err != nil {
			return nil, err
		}
	}
	return d, nil
}

// GetIncident loads one incident including phase metrics. Callers must have
// case scope (membership or admin).
func (s *Service) GetIncident(ctx context.Context, caller *store.User, id uuid.UUID, full bool) (*IncidentDetail, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	q := store.New(tx)

	inc, err := q.GetIncident(ctx, id)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, domain.ErrNotFound
		}
		return nil, err
	}
	if _, err := s.requireCaseAccess(ctx, q, id, caller); err != nil {
		return nil, err
	}
	d, err := s.incidentDetail(ctx, q, inc, full)
	if err != nil {
		return nil, err
	}
	return d, tx.Commit(ctx)
}

// ListIncidents returns incidents visible to the caller (all for admins,
// only assigned cases otherwise).
func (s *Service) ListIncidents(ctx context.Context, caller *store.User, statusFilter string, limit, offset int) ([]IncidentView, int, error) {
	if statusFilter != "" && !domain.IsValidStatus(statusFilter) {
		return nil, 0, domain.ErrValidation
	}
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	q := store.New(s.pool)
	rows, err := q.ListIncidents(ctx, store.ListIncidentsParams{
		Column1: caller.Role == domain.RoleAdmin,
		Column2: caller.ID,
		Column3: statusFilter,
		Limit:   int32(limit),
		Offset:  int32(offset),
	})
	if err != nil {
		return nil, 0, err
	}
	out := make([]IncidentView, 0, len(rows))
	for _, r := range rows {
		out = append(out, incidentView(r))
	}
	return out, len(out), nil
}

func incidentView(inc store.Incident) IncidentView {
	return IncidentView{
		ID:             inc.ID.String(),
		Title:          inc.Title,
		Description:    inc.Description,
		Severity:       inc.Severity,
		Status:         inc.Status,
		Version:        inc.Version,
		RootCause:      inc.RootCause,
		LessonsLearned: inc.LessonsLearned,
		DetectedAt:     ts(inc.DetectedAt),
		ClosedAt:       tsPtr(inc.ClosedAt),
		CreatedBy:      inc.CreatedBy.String(),
		CreatedAt:      ts(inc.CreatedAt),
		UpdatedAt:      ts(inc.UpdatedAt),
	}
}
