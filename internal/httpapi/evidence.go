package httpapi

import (
	"net/http"

	"github.com/google/uuid"

	"sircc/internal/store"
)

// maxEvidencePerIncident caps text evidence per case. The cap is enforced
// while holding a FOR UPDATE lock on the incident row, so concurrent
// submissions serialize and cannot overshoot it.
const maxEvidencePerIncident = 50

type addEvidenceRequest struct {
	Content string `json:"content"`
}

func (s *Server) handleAddEvidence(w http.ResponseWriter, r *http.Request) {
	u := actor(r)
	if ae := requireRole(u, "analyst"); ae != nil {
		writeErr(w, ae)
		return
	}
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req addEvidenceRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.Content == "" {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "content is required"))
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	// Locking the incident row serializes concurrent evidence inserts for
	// this case, so the count check below cannot race past the cap.
	if _, err := q.GetIncidentForUpdate(r.Context(), pgUUID(id)); isNotFound(err) {
		writeErr(w, errNotFound)
		return
	} else if err != nil {
		writeErr(w, err)
		return
	}
	count, err := q.CountEvidence(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	if count >= maxEvidencePerIncident {
		writeErr(w, errOf(http.StatusUnprocessableEntity, "evidence_limit",
			"an incident can hold at most 50 evidence entries"))
		return
	}
	ev, err := q.InsertEvidence(r.Context(), store.InsertEvidenceParams{
		ID:         pgUUID(uuid.New()),
		IncidentID: pgUUID(id),
		Seq:        count + 1,
		Content:    req.Content,
		AuthorID:   u.ID,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, pgUUID(id), &u.ID, "evidence.added",
		map[string]any{"evidenceId": fromPGUUID(ev.ID).String(), "seq": ev.Seq}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, toEvidenceDTO(ev))
}

func (s *Server) handleListEvidence(w http.ResponseWriter, r *http.Request) {
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	if _, err := s.q.GetIncident(r.Context(), pgUUID(id)); isNotFound(err) {
		writeErr(w, errNotFound)
		return
	} else if err != nil {
		writeErr(w, err)
		return
	}
	rows, err := s.q.ListEvidenceByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]evidenceDTO, 0, len(rows))
	for _, e := range rows {
		out = append(out, toEvidenceDTO(e))
	}
	writeJSON(w, http.StatusOK, out)
}

// ---- corrections: evidence is immutable, notes are append-only ----

type addEvidenceNoteRequest struct {
	Content string `json:"content"`
}

func (s *Server) handleAddEvidenceNote(w http.ResponseWriter, r *http.Request) {
	u := actor(r)
	if ae := requireRole(u, "analyst"); ae != nil {
		writeErr(w, ae)
		return
	}
	evID, ae := urlUUID(r, "evidenceID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req addEvidenceNoteRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.Content == "" {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "content is required"))
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	ev, err := q.GetEvidence(r.Context(), pgUUID(evID))
	if isNotFound(err) {
		writeErr(w, errNotFound)
		return
	}
	if err != nil {
		writeErr(w, err)
		return
	}
	note, err := q.InsertEvidenceNote(r.Context(), store.InsertEvidenceNoteParams{
		ID:         pgUUID(uuid.New()),
		EvidenceID: pgUUID(evID),
		Content:    req.Content,
		AuthorID:   u.ID,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, ev.IncidentID, &u.ID, "evidence.note_added",
		map[string]any{"evidenceId": evID.String()}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, toEvidenceNoteDTO(note))
}
