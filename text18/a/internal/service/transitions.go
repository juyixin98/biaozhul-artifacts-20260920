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

// TransitionInput carries an optimistic lifecycle transition.
type TransitionInput struct {
	Action          string // one of triage, contain, eradicate, recover, review, close
	ExpectedVersion int64
	RootCause       string
	LessonsLearned  string
	RequestID       uuid.UUID
}

// actionToTarget maps a requested action to the status it produces.
var actionToTarget = map[string]string{
	"triage":    domain.StatusTriaged,
	"contain":   domain.StatusContained,
	"eradicate": domain.StatusEradicated,
	"recover":   domain.StatusRecovered,
	"review":    domain.StatusReviewed,
	"close":     domain.StatusClosed,
}

// allowedTransitionRoles states which role may perform each action. Global
// admins may always act; other roles additionally need case membership with
// the matching case role.
var allowedTransitionRoles = map[string]string{
	"triage":    domain.RoleAnalyst,
	"contain":   domain.RoleResponder,
	"eradicate": domain.RoleResponder,
	"recover":   domain.RoleResponder,
	"review":    domain.RoleAnalyst,
	"close":     domain.RoleAdmin,
}

// Transition performs one guarded forward lifecycle step. The incident row,
// its new phase row and the audit event are written in the same transaction
// as the version bump, so readers can never observe a half transition.
func (s *Service) Transition(ctx context.Context, caller *store.User,
	incidentID uuid.UUID, in TransitionInput,
) (Result, error) {
	target, ok := actionToTarget[in.Action]
	if !ok {
		return Result{}, domain.ErrValidation
	}
	requiredRole := allowedTransitionRoles[in.Action]
	in.RootCause = strings.TrimSpace(in.RootCause)
	in.LessonsLearned = strings.TrimSpace(in.LessonsLearned)
	incident := incidentID
	path := "/api/v1/incidents/" + incidentID.String() + "/transitions"

	return s.runMutated(ctx, caller.ID, in.RequestID, &incident, "POST", path,
		func(ctx context.Context, q *store.Queries) (any, int, error) {
			inc, err := q.GetIncidentForUpdate(ctx, incidentID)
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}

			// Authorization: global admins always pass; every other role
			// must be a case member with the required case role.
			if caller.Role != domain.RoleAdmin {
				if _, err := s.requireCaseAccess(ctx, q, incidentID, caller, requiredRole); err != nil {
					return nil, 0, err
				}
			}

			// Optimistic concurrency.
			if inc.Version != in.ExpectedVersion {
				return nil, 0, domain.ErrStaleVersion
			}

			// Strict forward pipeline: only the immediate next status is allowed.
			if domain.NextStatus(inc.Status) != target {
				return nil, 0, domain.ErrInvalidTransition
			}

			// Gate: P1 cannot be triaged without an assigned responder.
			if target == domain.StatusTriaged && inc.Severity == "P1" {
				n, err := q.CountMembersByRole(ctx, store.CountMembersByRoleParams{
					IncidentID: incidentID,
					CaseRole:   domain.RoleResponder,
				})
				if err != nil {
					return nil, 0, err
				}
				if n == 0 {
					return nil, 0, domain.ErrTriageGate
				}
			}

			// Gate: closure needs root cause, lessons learned and at least
			// one action item carrying an owner and a deadline.
			if target == domain.StatusClosed {
				if in.RootCause == "" || in.LessonsLearned == "" {
					return nil, 0, domain.ErrClosureGate
				}
				n, err := q.CountEffectiveActionItems(ctx, incidentID)
				if err != nil {
					return nil, 0, err
				}
				if n == 0 {
					return nil, 0, domain.ErrClosureGate
				}
			}

			now := s.clock.Now()
			updated, err := q.AdvanceIncident(ctx, store.AdvanceIncidentParams{
				ID:             incidentID,
				Version:        in.ExpectedVersion,
				Status:         target,
				RootCause:      optionalStr(in.RootCause),
				LessonsLearned: optionalStr(in.LessonsLearned),
				UpdatedAt:      pgTimestamp(now),
			})
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrStaleVersion
				}
				return nil, 0, err
			}

			if _, err := q.CreatePhase(ctx, store.CreatePhaseParams{
				IncidentID: incidentID,
				Phase:      domain.StatusToPhase[target],
				ActorID:    caller.ID,
				RequestID:  pgUUID(in.RequestID),
				EnteredAt:  pgTimestamp(now),
			}); err != nil {
				return nil, 0, err
			}

			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: incidentID,
				ActorID:    caller.ID,
				Action:     "incident.transition." + in.Action,
				FromStatus: strPtr(inc.Status),
				ToStatus:   strPtr(target),
				RequestID:  pgUUID(in.RequestID),
				Detail:     []byte(`{"version":` + itoa(updated.Version) + `}`),
			}); err != nil {
				return nil, 0, err
			}

			detail, err := s.incidentDetail(ctx, q, updated, false)
			if err != nil {
				return nil, 0, err
			}
			return detail, http.StatusOK, nil
		})
}

func optionalStr(v string) *string {
	if v == "" {
		return nil
	}
	return &v
}

// AssignMember adds or updates a case member. Only global admins may do
// this, which is also how a P1 case gets its responder before triage.
type AssignMemberInput struct {
	UserID   string
	CaseRole string
}

func (s *Service) AssignMember(ctx context.Context, caller *store.User,
	incidentID uuid.UUID, userID uuid.UUID, caseRole string,
) (Result, error) {
	if caller.Role != domain.RoleAdmin {
		return Result{}, domain.ErrForbidden
	}
	switch caseRole {
	case domain.RoleAnalyst, domain.RoleResponder, domain.RoleAdmin:
	default:
		return Result{}, domain.ErrValidation
	}
	incident := incidentID

	return s.runMutated(ctx, caller.ID, uuid.Nil, &incident, "PUT",
		"/api/v1/incidents/"+incidentID.String()+"/members/"+userID.String(),
		func(ctx context.Context, q *store.Queries) (any, int, error) {
			inc, err := q.GetIncidentForUpdate(ctx, incidentID)
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			target, err := q.UserByID(ctx, userID)
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if target.Role != caseRole && caseRole != domain.RoleAdmin {
				// Case role must agree with the user's global role
				// (analysts stay analysts, responders stay responders).
				return nil, 0, domain.ErrValidation
			}
			if err := q.AddMember(ctx, store.AddMemberParams{
				IncidentID: incidentID,
				UserID:     userID,
				CaseRole:   caseRole,
				AssignedBy: pgUUID(caller.ID),
			}); err != nil {
				return nil, 0, err
			}
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: incidentID,
				ActorID:    caller.ID,
				Action:     "incident.member.assigned",
				FromStatus: strPtr(inc.Status),
				ToStatus:   strPtr(inc.Status),
				Detail:     []byte(`{"user_id":"` + userID.String() + `","case_role":"` + caseRole + `"}`),
			}); err != nil {
				return nil, 0, err
			}
			return map[string]any{
				"incident_id": incidentID.String(),
				"user_id":     userID.String(),
				"case_role":   caseRole,
			}, http.StatusOK, nil
		})
}
