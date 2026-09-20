package service

import (
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/db"
	"sircc/internal/domain"
	"sircc/internal/httpx"
	"sircc/internal/middleware"
)

// MaxEvidencePerIncident caps text evidence rows per case (hard requirement).
const MaxEvidencePerIncident = 50

type addEvidenceRequest struct {
	Content string `json:"content"`
}

func (s *Service) AddEvidence(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())

	var req addEvidenceRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.Content == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "evidence content is required")
		return
	}
	if !domain.CanAddEvidence(actor.Role) {
		httpx.ErrorJSON(w, http.StatusForbidden, httpx.CodeForbidden, "only analysts add evidence")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		// Scope check inside the tx.
		if actor.Role != domain.RoleAdmin {
			member, _ := q.IsIncidentMember(ctx, db.IsIncidentMemberParams{
				IncidentID: incidentID, UserID: actor.ID,
			})
			if !member {
				return errResult(http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			}
		}

		// Lock the incident row, then count. Two concurrent inserts serialize
		// on the row lock, so both cannot observe 49 and both succeed.
		if _, err := q.GetIncidentForUpdate(ctx, incidentID); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return errResult(http.StatusNotFound, httpx.CodeNotFound, "incident not found")
			}
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "lock incident")
		}
		cnt, err := q.CountEvidence(ctx, incidentID)
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "count evidence")
		}
		if cnt >= MaxEvidencePerIncident {
			return errResult(http.StatusUnprocessableEntity, httpx.CodeCapacity,
				"evidence limit (50) reached for this incident")
		}

		ev, err := q.AddEvidence(ctx, db.AddEvidenceParams{
			ID:          uuid.NewString(),
			IncidentID:  incidentID,
			Content:     req.Content,
			SubmittedBy: actor.ID,
			RequestID:   requestID,
		})
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "add evidence")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(incidentID), ActorID: actor.ID,
			Action: "evidence.added",
			Detail: mustJSON(map[string]string{"evidence_id": ev.ID}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(ev)
	})
}

func (s *Service) ListEvidence(w http.ResponseWriter, r *http.Request) {
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())
	if !s.canRead(w, r, actor, incidentID) {
		return
	}
	rows, err := s.q.ListEvidence(r.Context(), incidentID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "list evidence")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"evidence": rows, "count": len(rows), "limit": MaxEvidencePerIncident})
}

type addNoteRequest struct {
	Content string `json:"content"`
}

// AddEvidenceNote appends a correction/clarification. Existing evidence is
// never updated or overwritten.
func (s *Service) AddEvidenceNote(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	evidenceID := chi.URLParam(r, "evidenceID")
	actor, _ := actorFrom(r.Context())

	var req addNoteRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.Content == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "note content is required")
		return
	}
	if !domain.CanAddEvidence(actor.Role) {
		httpx.ErrorJSON(w, http.StatusForbidden, httpx.CodeForbidden, "only analysts annotate evidence")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		ev, err := q.GetEvidence(ctx, evidenceID)
		if err != nil {
			return errResult(http.StatusNotFound, httpx.CodeNotFound, "evidence not found")
		}
		if actor.Role != domain.RoleAdmin {
			member, _ := q.IsIncidentMember(ctx, db.IsIncidentMemberParams{
				IncidentID: ev.IncidentID, UserID: actor.ID,
			})
			if !member {
				return errResult(http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			}
		}
		note, err := q.AddEvidenceNote(ctx, db.AddEvidenceNoteParams{
			ID:         uuid.NewString(),
			EvidenceID: evidenceID,
			Content:    req.Content,
			AddedBy:    actor.ID,
			RequestID:  requestID,
		})
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "add note")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(ev.IncidentID), ActorID: actor.ID,
			Action: "evidence.note_added",
			Detail: mustJSON(map[string]string{"evidence_id": ev.ID, "note_id": note.ID}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(note)
	})
}
