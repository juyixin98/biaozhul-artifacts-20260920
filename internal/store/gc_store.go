package store

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
)

// GCRunRecord is the persisted state of one garbage-collection run.
type GCRunRecord struct {
	ID                string
	StartedAt         time.Time
	FinishedAt        *time.Time
	State             string
	MarkSnapshot      *int64
	MarkedAt          *time.Time
	GraceSeconds      int
	BlobsTotal        int
	ManifestsTotal    int
	DeletedBlobs      int
	DeletedManifests  int
	RetainedBlobs     int
	RetainedManifests int
	CrashedAt         *time.Time
	CrashReason       string
	Note              string
}

func (s *Store) InsertGCRun(ctx context.Context, id string, graceSeconds int) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO gc_runs(id, state, grace_seconds) VALUES ($1,'marking',$2)`,
		id, graceSeconds)
	return err
}

func (s *Store) MarkGCSweeping(ctx context.Context, id string, snapshot int64, markedAt time.Time,
	blobsTotal, manifestsTotal int) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE gc_runs SET state='sweeping', mark_snapshot=$2, marked_at=$3,
		 blobs_total=$4, manifests_total=$5 WHERE id=$1`,
		id, snapshot, markedAt, blobsTotal, manifestsTotal)
	return err
}

func (s *Store) CompleteGCRun(ctx context.Context, id string, deletedBlobs, deletedManifests,
	retainedBlobs, retainedManifests int, finishedAt time.Time) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE gc_runs SET state='completed', finished_at=$2,
		 deleted_blobs=$3, deleted_manifests=$4, retained_blobs=$5, retained_manifests=$6
		 WHERE id=$1`,
		id, finishedAt, deletedBlobs, deletedManifests, retainedBlobs, retainedManifests)
	return err
}

func (s *Store) MarkGCCrashed(ctx context.Context, id, reason string, at time.Time) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE gc_runs SET crashed_at=$2, crash_reason=$3 WHERE id=$1 AND state <> 'completed'`,
		id, at, reason)
	return err
}

func (s *Store) MarkGCRecovered(ctx context.Context, id string, note string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE gc_runs SET state='recovered', finished_at=now(), note=$2
		 WHERE id=$1 AND state <> 'completed'`, id, note)
	return err
}

func (s *Store) GetGCRun(ctx context.Context, id string) (*GCRunRecord, error) {
	var r GCRunRecord
	err := s.pool.QueryRow(ctx,
		`SELECT id, started_at, finished_at, state, mark_snapshot, marked_at, grace_seconds,
		        blobs_total, manifests_total, deleted_blobs, deleted_manifests,
		        retained_blobs, retained_manifests, crashed_at, COALESCE(crash_reason,''), COALESCE(note,'')
		 FROM gc_runs WHERE id=$1`, id).
		Scan(&r.ID, &r.StartedAt, &r.FinishedAt, &r.State, &r.MarkSnapshot, &r.MarkedAt, &r.GraceSeconds,
			&r.BlobsTotal, &r.ManifestsTotal, &r.DeletedBlobs, &r.DeletedManifests,
			&r.RetainedBlobs, &r.RetainedManifests, &r.CrashedAt, &r.CrashReason, &r.Note)
	if err == pgx.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// ListUnfinishedRuns returns runs still in marking/sweeping (crash candidates).
func (s *Store) ListUnfinishedRuns(ctx context.Context) ([]GCRunRecord, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, started_at, finished_at, state, mark_snapshot, marked_at, grace_seconds,
		        blobs_total, manifests_total, deleted_blobs, deleted_manifests,
		        retained_blobs, retained_manifests, crashed_at, COALESCE(crash_reason,''), COALESCE(note,'')
		 FROM gc_runs WHERE state IN ('marking','sweeping') ORDER BY started_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []GCRunRecord
	for rows.Next() {
		var r GCRunRecord
		if err := rows.Scan(&r.ID, &r.StartedAt, &r.FinishedAt, &r.State, &r.MarkSnapshot, &r.MarkedAt,
			&r.GraceSeconds, &r.BlobsTotal, &r.ManifestsTotal, &r.DeletedBlobs, &r.DeletedManifests,
			&r.RetainedBlobs, &r.RetainedManifests, &r.CrashedAt, &r.CrashReason, &r.Note); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Store) UpsertGCItem(ctx context.Context, tx pgx.Tx, runID, kind, repo, digest, decision, reason string) error {
	_, err := tx.Exec(ctx,
		`INSERT INTO gc_items(run_id, kind, repo, digest, decision, reason)
		 VALUES ($1,$2,$3,$4,$5,$6)
		 ON CONFLICT (run_id, kind, repo, digest)
		 DO UPDATE SET decision=EXCLUDED.decision, reason=EXCLUDED.reason, decided_at=now()`,
		runID, kind, repo, digest, decision, reason)
	return err
}

// GCItem is one audited object decision.
type GCItem struct {
	Kind      string    `json:"kind"`
	Repo      string    `json:"repo"`
	Digest    string    `json:"digest"`
	Decision  string    `json:"decision"`
	Reason    string    `json:"reason"`
	DecidedAt time.Time `json:"decided_at"`
}

func (s *Store) ListGCItems(ctx context.Context, runID string) ([]GCItem, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT kind, repo, digest, decision, reason, decided_at
		 FROM gc_items WHERE run_id=$1 ORDER BY kind, repo, digest`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []GCItem
	for rows.Next() {
		var it GCItem
		if err := rows.Scan(&it.Kind, &it.Repo, &it.Digest, &it.Decision, &it.Reason, &it.DecidedAt); err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	return out, rows.Err()
}

func (s *Store) AuditEvent(ctx context.Context, runID, action, kind, repo, digest, detail string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO gc_audit_events(run_id, action, kind, repo, digest, detail)
		 VALUES ($1,$2,$3,$4,$5,$6)`,
		runID, action, kind, repo, digest, detail)
	return err
}

func (s *Store) ListAuditEvents(ctx context.Context, runID string, limit int) ([]map[string]any, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, at, action, COALESCE(kind,''), COALESCE(repo,''), COALESCE(digest,''), COALESCE(detail,'')
		 FROM gc_audit_events WHERE ($1 = '' OR run_id=$1) ORDER BY id DESC LIMIT $2`, runID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id int64
		var at time.Time
		var action, kind, repo, digest, detail string
		if err := rows.Scan(&id, &at, &action, &kind, &repo, &digest, &detail); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			"id": id, "at": at, "action": action, "kind": kind,
			"repo": repo, "digest": digest, "detail": detail,
		})
	}
	return out, rows.Err()
}
