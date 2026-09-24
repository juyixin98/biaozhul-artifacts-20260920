package registry

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strconv"
	"time"

	"layerregistry/internal/gc"
	"layerregistry/internal/store"
)

// gcRequest controls a GC run. All fields are optional.
type gcRequest struct {
	GraceSeconds int `json:"grace_seconds"`
	// Fault injection (honored only when Config.FaultsEnabled):
	Fault          string `json:"fault"`            // "", "before_commit", "after_commit"
	MarkSweepDelay int    `json:"mark_sweep_delay"` // ms to pause between mark and sweep
}

// collectorFor builds a collector whose hooks honor fault headers. With
// faults disabled the hooks are nil and runs are production-faithful.
func (s *Server) collectorFor(fault string, markSweepDelay int, grace time.Duration) *gc.Collector {
	hooks := gc.Hooks{}
	if s.cfg.FaultsEnabled && markSweepDelay > 0 {
		d := time.Duration(markSweepDelay) * time.Millisecond
		hooks.BetweenMarkSweep = func(runID string) {
			time.Sleep(d)
		}
	}
	if s.cfg.FaultsEnabled {
		switch fault {
		case "before_commit":
			hooks.BeforeSweepCommit = func(runID, kind, repo, digest string) error {
				return errInjectedFault("crash before commit (fault=before_commit)")
			}
		case "after_commit":
			hooks.AfterSweepCommit = func(runID, kind, repo, digest string) error {
				return errInjectedFault("crash after commit (fault=after_commit)")
			}
		case "kill_before_commit":
			// Hard process kill AFTER quarantine, BEFORE tx commit. The file is
			// in quarantine while the DB row survives (tx rolls back on
			// disconnect); restart recovery must restore it.
			hooks.BeforeSweepCommit = func(runID, kind, repo, digest string) error {
				_ = s.st.MarkGCCrashed(context.Background(), runID,
					"simulated SIGKILL before commit: "+digest, time.Now())
				os.Exit(42)
				return nil
			}
		case "kill_after_commit":
			// Hard process kill AFTER tx commit, BEFORE purge. The row is
			// deleted durably while the file sits in quarantine; restart
			// recovery must purge it.
			hooks.AfterSweepCommit = func(runID, kind, repo, digest string) error {
				_ = s.st.MarkGCCrashed(context.Background(), runID,
					"simulated SIGKILL after commit: "+digest, time.Now())
				os.Exit(42)
				return nil
			}
		}
	}
	return gc.New(s.st, s.fs, gc.Config{BlobGrace: grace, Hooks: hooks})
}

type injectedFault string

func (e injectedFault) Error() string { return string(e) }

func errInjectedFault(msg string) error { return injectedFault(msg) }

func (s *Server) runGC(w http.ResponseWriter, r *http.Request) {
	req := gcRequest{}
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&req)
	}
	// Query params also accepted for easy curl use.
	if q := r.URL.Query().Get("fault"); q != "" {
		req.Fault = q
	}
	if q := r.URL.Query().Get("mark_sweep_delay"); q != "" {
		if ms, err := strconv.Atoi(q); err == nil {
			req.MarkSweepDelay = ms
		}
	}

	grace := s.cfg.BlobGrace
	if req.GraceSeconds > 0 {
		grace = time.Duration(req.GraceSeconds) * time.Second
	}

	col := s.collectorFor(req.Fault, req.MarkSweepDelay, grace)

	// Serialize GC cluster-wide: at most one mark/sweep run at a time. A
	// session advisory lock is used (Postgres auto-releases it if this process
	// is killed, so a simulated crash never wedges future runs).
	conn, err := s.st.Pool().Acquire(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "GC_FAILED", err.Error())
		return
	}
	locked, err := s.st.TryGCLock(r.Context(), conn)
	if err != nil {
		conn.Release()
		writeErr(w, http.StatusInternalServerError, "GC_FAILED", err.Error())
		return
	}
	if !locked {
		conn.Release()
		writeErr(w, http.StatusConflict, "GC_IN_PROGRESS", "another garbage collection run is in progress")
		return
	}
	defer func() {
		_ = s.st.ReleaseGCLock(context.Background(), conn)
		conn.Release()
	}()

	runID := newID()
	start := time.Now()
	res, err := col.Run(r.Context(), runID, start)
	if err != nil {
		var inj injectedFault
		if errors.As(err, &inj) {
			// A simulated crash: report the run as interrupted with its id so
			// the caller can recover.
			_ = s.st.MarkGCCrashed(r.Context(), runID, inj.Error(), time.Now())
			writeJSON(w, http.StatusConflict, map[string]any{
				"run_id": runID,
				"state":  "interrupted",
				"fault":  inj.Error(),
				"hint":   "the simulated crash interrupted the run; call POST /admin/recover then inspect GET /admin/gc/" + runID,
			})
			return
		}
		writeErr(w, http.StatusInternalServerError, "GC_FAILED", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) getGCRun(w http.ResponseWriter, r *http.Request) {
	rec, err := s.st.GetGCRun(r.Context(), r.PathValue("id"))
	if err != nil {
		mapStoreError(w, err)
		return
	}
	items, err := s.st.ListGCItems(r.Context(), rec.ID)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run": rec, "items": items})
}

func (s *Server) listGCRuns(w http.ResponseWriter, r *http.Request) {
	rows, err := s.st.Pool().Query(r.Context(),
		`SELECT id, started_at, COALESCE(finished_at, to_timestamp(0)), state,
		        COALESCE(mark_snapshot,0), deleted_blobs, deleted_manifests,
		        retained_blobs, retained_manifests
		 FROM gc_runs ORDER BY started_at DESC LIMIT 50`)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "INTERNAL", err.Error())
		return
	}
	defer rows.Close()
	type runSummary struct {
		ID                string     `json:"id"`
		StartedAt         time.Time  `json:"started_at"`
		FinishedAt        *time.Time `json:"finished_at"`
		State             string     `json:"state"`
		MarkSnapshot      int64      `json:"mark_snapshot"`
		DeletedBlobs      int        `json:"deleted_blobs"`
		DeletedManifests  int        `json:"deleted_manifests"`
		RetainedBlobs     int        `json:"retained_blobs"`
		RetainedManifests int        `json:"retained_manifests"`
	}
	out := []runSummary{}
	for rows.Next() {
		var v runSummary
		var finishedAt time.Time
		if err := rows.Scan(&v.ID, &v.StartedAt, &finishedAt, &v.State, &v.MarkSnapshot,
			&v.DeletedBlobs, &v.DeletedManifests, &v.RetainedBlobs, &v.RetainedManifests); err != nil {
			writeErr(w, http.StatusInternalServerError, "INTERNAL", err.Error())
			return
		}
		if !finishedAt.IsZero() {
			v.FinishedAt = &finishedAt
		}
		out = append(out, v)
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": out})
}

func (s *Server) cleanupOrphans(w http.ResponseWriter, r *http.Request) {
	cutoff := time.Now().Add(-s.cfg.OrphanMaxAge)
	res, err := s.col.CleanupOrphans(r.Context(), cutoff)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "ORPHAN_FAILED", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) listBlobs(w http.ResponseWriter, r *http.Request) {
	blobs, err := s.st.ListBlobs(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "INTERNAL", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"blobs": blobs})
}

func (s *Server) audit(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if q := r.URL.Query().Get("limit"); q != "" {
		if n, err := strconv.Atoi(q); err == nil && n > 0 && n <= 1000 {
			limit = n
		}
	}
	runID := r.URL.Query().Get("run_id")
	events, err := s.st.ListAuditEvents(r.Context(), runID, limit)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "INTERNAL", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": events})
}

// recover runs crash recovery and reports exactly what was reconciled.
func (s *Server) recover(w http.ResponseWriter, r *http.Request) {
	logLines, err := s.col.Recover(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "RECOVER_FAILED", err.Error())
		return
	}
	strays, err := s.col.ReconcileBlobFiles(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "RECONCILE_FAILED", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"recovered":                       logLines,
		"stray_blob_files_without_db_row": strays,
	})
}

var _ = store.ErrNotFound
