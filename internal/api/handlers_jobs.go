package api

import (
	"encoding/json"
	"net/http"
	"os"

	"github.com/jackc/pgx/v5"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
)

type createJobRequest struct {
	Priority   int `json:"priority"`
	FrameStart int `json:"frameStart"`
	FrameEnd   int `json:"frameEnd"`
}

// createJob binds a render task to a frozen version. It validates project
// membership and the frame range, inserts the job and one pending frame row
// per frame, all in a single transaction.
func (s *Server) createJob(w http.ResponseWriter, r *http.Request) {
	vid, ok := parseUUID(w, r, "versionID")
	if !ok {
		return
	}
	v, err := s.q.GetVersion(r.Context(), vid)
	if err != nil {
		writeError(w, http.StatusNotFound, "version not found")
		return
	}
	c, err := s.q.GetComposition(r.Context(), v.CompositionID)
	if err != nil {
		writeError(w, http.StatusNotFound, "composition not found")
		return
	}
	if !s.authorizeProject(w, r, c.ProjectID) {
		return
	}
	u := currentUser(r)

	req := createJobRequest{Priority: 5, FrameStart: 0, FrameEnd: 0}
	if r.ContentLength != 0 {
		if err := decodeJSON(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid job JSON: "+err.Error())
			return
		}
	}
	if req.Priority < 1 || req.Priority > 10 {
		writeError(w, http.StatusBadRequest, "priority must be between 1 and 10")
		return
	}
	if req.FrameStart < 0 || req.FrameEnd < req.FrameStart {
		writeError(w, http.StatusBadRequest, "frame range invalid (need 0 <= start <= end)")
		return
	}
	if req.FrameEnd-req.FrameStart > 100000 {
		writeError(w, http.StatusBadRequest, "at most 100001 frames per job")
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)

	job, err := qtx.CreateJob(r.Context(), dbgen.CreateJobParams{
		VersionID: vid, ProjectID: c.ProjectID, CreatedBy: u.ID,
		Priority:   int32(req.Priority),
		FrameStart: int32(req.FrameStart), FrameEnd: int32(req.FrameEnd),
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for f := req.FrameStart; f <= req.FrameEnd; f++ {
		if _, err := qtx.InsertFrame(r.Context(), dbgen.InsertFrameParams{
			JobID: job.ID, FrameNo: int32(f), MaxAttempts: 4,
		}); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, jobDTOFrom(job))
}

// loadJobForAccess fetches a job and enforces access: project members (and
// admins) may read; writes are checked in the specific handlers.
func (s *Server) loadJobForAccess(w http.ResponseWriter, r *http.Request) (dbgen.Job, bool) {
	jid, ok := parseUUID(w, r, "jobID")
	if !ok {
		return dbgen.Job{}, false
	}
	job, err := s.q.GetJob(r.Context(), jid)
	if err == pgx.ErrNoRows {
		writeError(w, http.StatusNotFound, "job not found")
		return dbgen.Job{}, false
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return dbgen.Job{}, false
	}
	if !s.authorizeProject(w, r, job.ProjectID) {
		return dbgen.Job{}, false
	}
	return job, true
}

func (s *Server) getJob(w http.ResponseWriter, r *http.Request) {
	job, ok := s.loadJobForAccess(w, r)
	if !ok {
		return
	}
	counts, err := s.q.CountFrameStatuses(r.Context(), job.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	dto := jobDTOFrom(job)
	dto.FrameCounts = map[string]int64{
		"pending":   counts.Pending,
		"leased":    counts.Leased,
		"succeeded": counts.Succeeded,
		"failed":    counts.Failed,
		"total":     counts.Total,
	}
	writeJSON(w, http.StatusOK, dto)
}

func (s *Server) listJobFrames(w http.ResponseWriter, r *http.Request) {
	job, ok := s.loadJobForAccess(w, r)
	if !ok {
		return
	}
	frames, err := s.q.ListFramesOfJob(r.Context(), job.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]frameDTO, 0, len(frames))
	for _, f := range frames {
		out = append(out, frameDTOFrom(f))
	}
	writeJSON(w, http.StatusOK, out)
}

// cancelJob implements the single-terminal-state rule. Admins may cancel
// any job; members may only cancel jobs in their own projects. The UPDATE
// is guarded by status IN ('queued','running'), so a cancel racing with the
// final completion affects either 1 row (cancel wins) or 0 (job already
// terminal). Leased frames are requeued with a generation bump, which makes
// any in-flight worker's later submit a no-op.
func (s *Server) cancelJob(w http.ResponseWriter, r *http.Request) {
	job, ok := s.loadJobForAccess(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	if !isAdmin(u) {
		p, err := s.q.GetProject(r.Context(), job.ProjectID)
		if err != nil || p.OwnerID != u.ID {
			// Members can read a shared project but only the owner (or
			// admin) cancels. This is stricter than read access.
			writeError(w, http.StatusForbidden, "only an admin or the project owner may cancel")
			return
		}
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)

	// Flip the job FIRST so lock acquisition order is job-row then
	// frame-rows, matching the worker submit path (FOR UPDATE job -> frame)
	// and avoiding a deadlock against a concurrent claim (frame -> job).
	// A claim racing us blocks on this job-row lock and, once unblocked,
	// finds the job canceled so its guarded updates match nothing.
	status, err := qtx.CancelJob(r.Context(), dbgen.CancelJobParams{
		ID: job.ID, CanceledBy: &u.ID,
	})
	if err == pgx.ErrNoRows {
		tx.Rollback(r.Context())
		// Already terminal: report the existing state instead of inventing
		// a cancel.
		cur, _ := s.q.GetJob(r.Context(), job.ID)
		writeJSON(w, http.StatusConflict, map[string]string{
			"error":  "job already terminal",
			"status": cur.Status,
		})
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	// Invalidate any frame still leased when cancel committed; the
	// generation bump makes that worker's heartbeat and submit no-ops.
	if err := qtx.RequeueLeasedFramesOfJob(r.Context(), job.ID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	// Best effort: remove partial outputs of a canceled job so they cannot
	// be mistaken for deliverables. Completed frame rows are retained for
	// audit; only the transient files go.
	_ = s.store.CleanupJobDir(job.ID.String())
	writeJSON(w, http.StatusOK, map[string]string{"id": job.ID.String(), "status": status})
}

func (s *Server) getFramePNG(w http.ResponseWriter, r *http.Request) {
	job, ok := s.loadJobForAccess(w, r)
	if !ok {
		return
	}
	frameNo, ok2 := parseIntURL(w, r, "frameNo")
	if !ok2 {
		return
	}
	path := s.store.FramePath(job.ID.String(), frameNo)
	if _, err := os.Stat(path); err != nil {
		writeError(w, http.StatusNotFound, "frame output not available")
		return
	}
	w.Header().Set("Content-Type", "image/png")
	http.ServeFile(w, r, path)
}

func (s *Server) getJobSummary(w http.ResponseWriter, r *http.Request) {
	job, ok := s.loadJobForAccess(w, r)
	if !ok {
		return
	}
	if job.Status != "succeeded" {
		writeError(w, http.StatusConflict, "summary available only for succeeded jobs (status: "+job.Status+")")
		return
	}
	data, err := os.ReadFile(s.store.SummaryPath(job.ID.String()))
	if err != nil {
		writeError(w, http.StatusNotFound, "summary file not found")
		return
	}
	var v any
	if err := json.Unmarshal(data, &v); err != nil {
		writeError(w, http.StatusInternalServerError, "corrupt summary")
		return
	}
	writeJSON(w, http.StatusOK, v)
}
