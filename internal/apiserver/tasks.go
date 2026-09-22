package apiserver

import (
	"errors"
	"net/http"
	"path/filepath"
	"strconv"

	"vfxqueue/internal/auth"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/idgen"
	"vfxqueue/internal/storage"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type enqueueReq struct {
	CompositionID uuid.UUID  `json:"composition_id"`
	VersionID     *uuid.UUID `json:"version_id,omitempty"` // default: current
	FrameStart    int        `json:"frame_start"`
	FrameEnd      int        `json:"frame_end"` // inclusive; -1 = last
	Priority      int        `json:"priority"`  // 1..10, 10 highest
}

func (s *Server) enqueueTask(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	u := auth.User(r.Context())

	var req enqueueReq
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, 400, "invalid body: "+err.Error())
		return
	}
	if req.Priority < 1 || req.Priority > 10 {
		writeErr(w, 400, "priority must be between 1 and 10")
		return
	}
	comp, err := s.q.GetComposition(r.Context(), req.CompositionID)
	if err != nil {
		writeErr(w, 404, "composition not found")
		return
	}
	if comp.ProjectID != proj.ID {
		writeErr(w, 404, "composition not found in this project")
		return
	}
	versionID := comp.CurrentVersionID
	if req.VersionID != nil {
		versionID = req.VersionID
	}
	if versionID == nil {
		writeErr(w, 409, "composition has no frozen version")
		return
	}
	ver, err := s.q.GetVersion(r.Context(), *versionID)
	if err != nil || ver.CompositionID != comp.ID {
		writeErr(w, 404, "version not found for this composition")
		return
	}
	last := int(comp.FrameCount) - 1
	fs, fe := req.FrameStart, req.FrameEnd
	if fe == -1 {
		fe = last
	}
	if fs < 0 || fe < fs || fe > last {
		writeErr(w, 400, "frame range must be within 0.."+strconv.Itoa(last))
		return
	}

	taskID := idgen.NewID()
	outDir := filepath.Join(s.cfg.OutputsDir, taskID.String())
	if err := storage.EnsureDir(outDir); err != nil {
		writeErr(w, 500, err.Error())
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)
	task, err := qtx.CreateTask(r.Context(), gen.CreateTaskParams{
		ID: taskID, ProjectID: proj.ID, CompositionID: comp.ID, VersionID: ver.ID,
		FrameStart: int32(fs), FrameEnd: int32(fe), Priority: int16(req.Priority),
		CreatedBy: u.ID, OutputDir: outDir,
	})
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	for f := fs; f <= fe; f++ {
		if err := qtx.CreateFrame(r.Context(), gen.CreateFrameParams{
			ID: idgen.NewID(), TaskID: task.ID, FrameIndex: int32(f),
		}); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 201, taskView(task, nil))
}

// loadTaskForUser fetches a task and enforces that it belongs to a project the
// caller may access. Admins may access (and cancel) any task.
func (s *Server) loadTaskForUser(w http.ResponseWriter, r *http.Request) (*gen.RenderTask, bool) {
	tid, err := uuid.Parse(chi.URLParam(r, "taskID"))
	if err != nil {
		writeErr(w, 400, "invalid task id")
		return nil, false
	}
	task, err := s.q.GetTask(r.Context(), tid)
	if err != nil {
		writeErr(w, 404, "task not found")
		return nil, false
	}
	u := auth.User(r.Context())
	if u.Role != "admin" {
		ok, err := auth.CanAccessProject(r.Context(), s.q, u, task.ProjectID)
		if err != nil {
			writeErr(w, 500, "authz error")
			return nil, false
		}
		if !ok {
			writeErr(w, 403, "task belongs to a project you cannot access")
			return nil, false
		}
	}
	return &task, true
}

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	task, ok := s.loadTaskForUser(w, r)
	if !ok {
		return
	}
	counts, err := s.q.FrameStatusCounts(r.Context(), task.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, taskView(*task, counts))
}

func (s *Server) listTasks(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	rows, err := s.q.ListProjectTasks(r.Context(), gen.ListProjectTasksParams{
		ProjectID: proj.ID, Limit: 100,
	})
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, rows)
}

func (s *Server) cancelTask(w http.ResponseWriter, r *http.Request) {
	task, ok := s.loadTaskForUser(w, r)
	if !ok {
		return
	}
	u := auth.User(r.Context())
	// Members may only cancel tasks in projects they belong to — loadTaskForUser
	// already proved membership. Admins may cancel anything. Members cannot
	// cancel tasks in other people's projects because access is denied above.
	_ = u

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)

	// Lock the task row first so a racing frame-complete submit serializes with
	// this cancellation.
	locked, err := qtx.LockTask(r.Context(), task.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if locked.Status == "cancelled" {
		_ = tx.Commit(r.Context())
		writeJSON(w, 200, taskView(locked, nil))
		return
	}
	if locked.Status == "succeeded" || locked.Status == "failed" {
		// Terminal task wins over a late cancel request.
		_ = tx.Commit(r.Context())
		writeErr(w, 409, "task already "+locked.Status)
		return
	}
	cancelled, err := qtx.CancelTask(r.Context(), task.ID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeErr(w, 409, "task became terminal concurrently")
			return
		}
		writeErr(w, 500, err.Error())
		return
	}
	// Bump generation and clear every non-terminal frame lease so any in-flight
	// worker's commit is rejected as stale. After cancel, no results publish.
	if _, err := qtx.CancelRemainingFrames(r.Context(), task.ID); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, taskView(cancelled, nil))
}

func (s *Server) listTaskFrames(w http.ResponseWriter, r *http.Request) {
	task, ok := s.loadTaskForUser(w, r)
	if !ok {
		return
	}
	rows, err := s.q.ListFrames(r.Context(), task.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, f := range rows {
		out = append(out, frameView(f))
	}
	writeJSON(w, 200, out)
}

func (s *Server) getFrame(w http.ResponseWriter, r *http.Request) {
	task, ok := s.loadTaskForUser(w, r)
	if !ok {
		return
	}
	idx, err := strconv.Atoi(chi.URLParam(r, "frameIndex"))
	if err != nil || idx < int(task.FrameStart) || idx > int(task.FrameEnd) {
		writeErr(w, 400, "frame index out of task range")
		return
	}
	f, err := s.q.GetFrameForTask(r.Context(), gen.GetFrameForTaskParams{
		TaskID: task.ID, FrameIndex: int32(idx),
	})
	if err != nil {
		writeErr(w, 404, "frame not found")
		return
	}
	writeJSON(w, 200, frameView(f))
}

func (s *Server) framePNG(w http.ResponseWriter, r *http.Request) {
	task, ok := s.loadTaskForUser(w, r)
	if !ok {
		return
	}
	idx, err := strconv.Atoi(chi.URLParam(r, "frameIndex"))
	if err != nil || idx < int(task.FrameStart) || idx > int(task.FrameEnd) {
		writeErr(w, 400, "frame index out of task range")
		return
	}
	f, err := s.q.GetFrameForTask(r.Context(), gen.GetFrameForTaskParams{
		TaskID: task.ID, FrameIndex: int32(idx),
	})
	if err != nil || f.Status != "succeeded" || f.OutputPath == nil {
		writeErr(w, 404, "frame not rendered")
		return
	}
	// Defense in depth: the stored path must stay inside this task output dir.
	if filepath.Dir(*f.OutputPath) != task.OutputDir {
		writeErr(w, 403, "path check failed")
		return
	}
	w.Header().Set("Content-Type", "image/png")
	http.ServeFile(w, r, *f.OutputPath)
}

func taskView(t gen.RenderTask, counts []gen.FrameStatusCountsRow) map[string]any {
	total := int(t.FrameEnd - t.FrameStart + 1)
	byStatus := map[string]int64{}
	for _, c := range counts {
		byStatus[c.Status] = c.Count
	}
	return map[string]any{
		"id": t.ID, "project_id": t.ProjectID, "composition_id": t.CompositionID,
		"version_id": t.VersionID, "frame_start": t.FrameStart, "frame_end": t.FrameEnd,
		"priority": t.Priority, "status": t.Status, "error": t.Error,
		"created_at": t.CreatedAt, "started_at": t.StartedAt, "finished_at": t.FinishedAt,
		"frames_total":     total,
		"frames_by_status": byStatus,
		"frames_succeeded": byStatus["succeeded"],
		"frames_failed":    byStatus["failed"],
	}
}

func frameView(f gen.Frame) map[string]any {
	v := map[string]any{
		"frame_index": f.FrameIndex, "status": f.Status, "attempts": f.Attempts,
		"generation": f.Generation, "updated_at": f.UpdatedAt, "error": f.Error,
	}
	if f.Status == "succeeded" {
		v["sha256"] = f.OutputSha256
		v["size_bytes"] = f.OutputSizeBytes
	}
	return v
}
