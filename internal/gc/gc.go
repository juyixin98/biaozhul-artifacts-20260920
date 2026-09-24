// Package gc implements the safe two-phase garbage collector.
//
// Safety model (the heart of the project):
//
//  1. MARK — a single REPEATABLE READ transaction takes the global publish
//     advisory lock before reading, then walks tags + active read leases
//     through the manifest DAG (recursive CTE) to produce the reachable set
//     at one consistent snapshot, recording pg_current_xact_id() as a fence.
//  2. SWEEP — every deletion candidate is re-verified in a FRESH transaction
//     that (a) takes the global publish lock — so any publish that committed
//     after the mark snapshot is now visible — and (b) takes the object's
//     per-blob/manifest advisory lock — so an in-flight pull's read lease
//     cannot land between check and delete — then recomputes reachability and
//     checks active leases. Only if all checks still say unreachable is the
//     object deleted. Therefore a layer referenced at any time before the
//     delete commits is retained.
//  3. CRASH ORDERING — the file is renamed into a per-run quarantine dir
//     BEFORE the database row is deleted. Recovery on next startup either
//     restores the file (row survived) or purges it (row deleted durably),
//     yielding an all-or-nothing outcome per object.
package gc

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"layerregistry/internal/store"
)

// Hooks are test/operation injection points. In production all are nil.
type Hooks struct {
	// BetweenMarkSweep is invoked once after mark, before the sweep starts.
	// Tests use it (via an HTTP delay header) to drive concurrent publishes
	// into the mark/sweep gap.
	BetweenMarkSweep func(runID string)
	// BeforeSweepCommit fires inside the delete transaction, after the row
	// has been deleted but BEFORE commit. Returning an error aborts the run
	// and rolls the transaction back (row survives while the file is already
	// quarantined) — the "restore on recovery" crash window. A hook that
	// calls os.Exit simulates a real kill in this same window.
	BeforeSweepCommit func(runID, kind, repo, digest string) error
	// AfterSweepCommit fires after the delete committed but before the
	// quarantined bytes are purged — the "purge on recovery" crash window.
	AfterSweepCommit func(runID, kind, repo, digest string) error
}

// Config configures a collector.
type Config struct {
	// BlobGrace keeps very recently uploaded blobs even if nothing points at
	// them yet (protects in-flight uploads). 0 disables.
	BlobGrace time.Duration
	Hooks     Hooks
}

// ItemDecision is one audited line in the report.
type ItemDecision struct {
	Kind     string `json:"kind"`
	Repo     string `json:"repo"`
	Digest   string `json:"digest"`
	Decision string `json:"decision"`
	Phase    string `json:"phase"` // "mark" retained/deleted-at-mark; "sweep" decision phase
	Reason   string `json:"reason"`
}

// Result is the full auditable outcome of a run.
type Result struct {
	RunID             string         `json:"run_id"`
	StartedAt         time.Time      `json:"started_at"`
	FinishedAt        time.Time      `json:"finished_at"`
	State             string         `json:"state"`
	MarkSnapshotXID   int64          `json:"mark_snapshot_xid"`
	MarkedAt          time.Time      `json:"marked_at"`
	Grace             time.Duration  `json:"grace"`
	Items             []ItemDecision `json:"items"`
	DeletedBlobs      int            `json:"deleted_blobs"`
	DeletedManifests  int            `json:"deleted_manifests"`
	RetainedBlobs     int            `json:"retained_blobs"`
	RetainedManifests int            `json:"retained_manifests"`
}

// Collector runs garbage collection against a store + on-disk storage.
type Collector struct {
	st  *store.Store
	fs  FileStore
	cfg Config
}

// FileStore is the storage subset the collector needs.
type FileStore interface {
	Quarantine(runID, digest string) error
	Restore(runID, digest string) error
	Purge(runID, digest string) error
	PurgeQuarantineDir(runID string) error
	RemoveBlob(digest string) error
	RemoveTemp(name string) error
}

func New(st *store.Store, fs FileStore, cfg Config) *Collector {
	return &Collector{st: st, fs: fs, cfg: cfg}
}

type markObject struct {
	kind   string
	repo   string
	digest string
}

// Run executes a full mark+sweep collection.
func (c *Collector) Run(ctx context.Context, runID string, now time.Time) (*Result, error) {
	if err := c.st.InsertGCRun(ctx, runID, int(c.cfg.BlobGrace.Seconds())); err != nil {
		return nil, fmt.Errorf("create gc run: %w", err)
	}
	res := &Result{RunID: runID, StartedAt: now, Grace: c.cfg.BlobGrace}
	_ = c.st.AuditEvent(ctx, runID, "gc.run_start", "", "", "",
		fmt.Sprintf("grace=%s", c.cfg.BlobGrace))

	// ---- MARK -----------------------------------------------------------
	var snapshot int64
	var markedAt time.Time
	var blobs []store.BlobInfo
	var allManifests []store.AllManifest
	var reach map[string]store.ReachRow

	{
		tx, err := c.st.Pool().Begin(ctx)
		if err != nil {
			return nil, err
		}
		defer tx.Rollback(ctx)

		// The publish lock inside the mark transaction is what stamps our
		// snapshot relative to every publisher: any publisher whose commit we
		// miss here must commit AFTER this lock release, and the sweep's
		// per-item publish lock will then make that commit visible.
		if err := store.TxPublishLock(ctx, tx); err != nil {
			return nil, err
		}
		snapshot, markedAt, err = store.AcquireGCxid(ctx, tx)
		if err != nil {
			return nil, err
		}
		if reach, err = store.ReachableSet(ctx, tx); err != nil {
			return nil, err
		}
		blobs, err = listBlobsTx(ctx, tx)
		if err != nil {
			return nil, err
		}
		allManifests, err = listManifestsTx(ctx, tx)
		if err != nil {
			return nil, err
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
	}
	res.MarkSnapshotXID = snapshot
	res.MarkedAt = markedAt

	// Classify against the snapshot and write the initial audit rows.
	var candidates []markObject
	for _, b := range blobs {
		key := store.SetKey("blob", "", b.Digest)
		if _, ok := reach[key]; ok {
			reason := c.retainReasonReachable(ctx, "blob", "", b.Digest, reach)
			res.Items = append(res.Items, ItemDecision{
				Kind: "blob", Digest: b.Digest, Decision: "retain",
				Phase: "mark", Reason: reason,
			})
			res.RetainedBlobs++
			c.auditItem(ctx, runID, "blob", "", b.Digest, "retain", reason)
			continue
		}
		if c.cfg.BlobGrace > 0 && now.Sub(b.CreatedAt) < c.cfg.BlobGrace {
			reason := fmt.Sprintf("retained: uploaded %s ago, within %s new-blob grace period",
				now.Sub(b.CreatedAt).Round(time.Second), c.cfg.BlobGrace)
			res.Items = append(res.Items, ItemDecision{
				Kind: "blob", Digest: b.Digest, Decision: "retain",
				Phase: "mark", Reason: reason,
			})
			res.RetainedBlobs++
			c.auditItem(ctx, runID, "blob", "", b.Digest, "retain", reason)
			continue
		}
		candidates = append(candidates, markObject{"blob", "", b.Digest})
	}
	for _, m := range allManifests {
		key := store.SetKey("manifest", m.Repo, m.Digest)
		if _, ok := reach[key]; ok {
			reason := c.retainReasonReachable(ctx, "manifest", m.Repo, m.Digest, reach)
			res.Items = append(res.Items, ItemDecision{
				Kind: "manifest", Repo: m.Repo, Digest: m.Digest, Decision: "retain",
				Phase: "mark", Reason: reason,
			})
			res.RetainedManifests++
			c.auditItem(ctx, runID, "manifest", m.Repo, m.Digest, "retain", reason)
			continue
		}
		candidates = append(candidates, markObject{"manifest", m.Repo, m.Digest})
	}

	if err := c.st.MarkGCSweeping(ctx, runID, snapshot, markedAt,
		len(blobs), len(allManifests)); err != nil {
		return nil, err
	}
	_ = c.st.AuditEvent(ctx, runID, "gc.mark_complete", "", "", "",
		fmt.Sprintf("snapshot_xid=%d blobs=%d manifests=%d candidates=%d",
			snapshot, len(blobs), len(allManifests), len(candidates)))

	if c.cfg.Hooks.BetweenMarkSweep != nil {
		c.cfg.Hooks.BetweenMarkSweep(runID)
	}

	// ---- SWEEP (re-verify every candidate against fresh state) ----------
	for _, obj := range candidates {
		decision, reason, err := c.sweepOne(ctx, runID, obj, snapshot)
		if err != nil {
			return res, fmt.Errorf("sweep %s %s/%s: %w", obj.kind, obj.repo, obj.digest, err)
		}
		res.Items = append(res.Items, ItemDecision{
			Kind: obj.kind, Repo: obj.repo, Digest: obj.digest,
			Decision: decision, Phase: "sweep", Reason: reason,
		})
		switch obj.kind {
		case "blob":
			if decision == "delete" {
				res.DeletedBlobs++
			} else {
				res.RetainedBlobs++
			}
		case "manifest":
			if decision == "delete" {
				res.DeletedManifests++
			} else {
				res.RetainedManifests++
			}
		}
	}

	res.FinishedAt = time.Now()
	res.State = "completed"
	if err := c.st.CompleteGCRun(ctx, runID, res.DeletedBlobs, res.DeletedManifests,
		res.RetainedBlobs, res.RetainedManifests, res.FinishedAt); err != nil {
		return res, err
	}
	if err := c.fs.PurgeQuarantineDir(runID); err != nil {
		return res, fmt.Errorf("purge quarantine dir: %w", err)
	}
	_ = c.st.AuditEvent(ctx, runID, "gc.run_complete", "", "", "",
		fmt.Sprintf("deleted blobs=%d manifests=%d retained blobs=%d manifests=%d",
			res.DeletedBlobs, res.DeletedManifests, res.RetainedBlobs, res.RetainedManifests))

	sort.SliceStable(res.Items, func(i, j int) bool {
		if res.Items[i].Kind != res.Items[j].Kind {
			return res.Items[i].Kind < res.Items[j].Kind
		}
		if res.Items[i].Repo != res.Items[j].Repo {
			return res.Items[i].Repo < res.Items[j].Repo
		}
		return res.Items[i].Digest < res.Items[j].Digest
	})
	return res, nil
}

// sweepOne performs the delete-time verification for one candidate.
func (c *Collector) sweepOne(ctx context.Context, runID string, obj markObject, fenceXID int64) (string, string, error) {
	// Crash-safe file ordering is needed only for blobs (manifests live as
	// BYTEA in the database): quarantine first, delete row after.
	if obj.kind == "blob" {
		if err := c.fs.Quarantine(runID, obj.digest); err != nil {
			return "", "", fmt.Errorf("quarantine: %w", err)
		}
	}

	tx, err := c.st.Pool().Begin(ctx)
	if err != nil {
		return "", "", err
	}
	defer tx.Rollback(ctx)

	// Global publish lock: total order against publishers (the fence).
	if err := store.TxPublishLock(ctx, tx); err != nil {
		return "", "", err
	}
	// Object lock: total order against in-flight pulls.
	if obj.kind == "blob" {
		if err := store.TxBlobLock(ctx, tx, obj.digest); err != nil {
			return "", "", err
		}
	} else {
		if err := store.TxManifestLock(ctx, tx, obj.repo, obj.digest); err != nil {
			return "", "", err
		}
	}

	// Does the object still exist?
	var exists bool
	if obj.kind == "blob" {
		err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM blobs WHERE digest=$1)", obj.digest).Scan(&exists)
	} else {
		err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM manifests WHERE repo=$1 AND digest=$2)",
			obj.repo, obj.digest).Scan(&exists)
	}
	if err != nil {
		return "", "", err
	}
	if !exists {
		// Someone (API delete) removed it concurrently. Release quarantine.
		if obj.kind == "blob" {
			if pErr := c.fs.Purge(runID, obj.digest); pErr != nil {
				return "", "", pErr
			}
		}
		reason := "retained-by-state: object already absent at sweep recheck"
		c.auditItemTx(ctx, tx, runID, obj, "retain", reason)
		return "retain", reason, nil
	}

	// Fresh reachability over current committed state.
	reach, err := store.ReachableSet(ctx, tx)
	if err != nil {
		return "", "", err
	}
	if _, ok := reach[store.SetKey(obj.kind, obj.repo, obj.digest)]; ok {
		reason := c.retainReasonReachable(ctx, obj.kind, obj.repo, obj.digest, reach)
		reason = "retained-after-mark: " + reason
		if err := tx.Commit(ctx); err != nil {
			return "", "", err
		}
		if obj.kind == "blob" {
			if rErr := c.fs.Restore(runID, obj.digest); rErr != nil {
				return "", "", fmt.Errorf("restore: %w", rErr)
			}
		}
		c.auditItem(ctx, runID, obj.kind, obj.repo, obj.digest, "retain", reason)
		return "retain", reason, nil
	}

	// Active read lease? (Defense in depth: the object lock already waits for
	// pull locks; leases can also be explicitly pinned by clients.)
	leases, err := c.activeLeases(ctx, obj)
	if err != nil {
		return "", "", err
	}
	if len(leases) > 0 {
		reason := fmt.Sprintf("retained-after-mark: active read lease(s) %s protect it at sweep recheck",
			strings.Join(leases, ","))
		if err := tx.Commit(ctx); err != nil {
			return "", "", err
		}
		if obj.kind == "blob" {
			if rErr := c.fs.Restore(runID, obj.digest); rErr != nil {
				return "", "", fmt.Errorf("restore: %w", rErr)
			}
		}
		c.auditItem(ctx, runID, obj.kind, obj.repo, obj.digest, "retain", reason)
		return "retain", reason, nil
	}

	// Still garbage: durably delete the database row.
	if obj.kind == "blob" {
		if err := c.st.DeleteBlobRow(ctx, tx, obj.digest); err != nil {
			return "", "", err
		}
	} else {
		if err := c.st.DeleteManifestCascade(ctx, tx, obj.repo, obj.digest); err != nil {
			return "", "", err
		}
	}
	if err := c.auditItemTx(ctx, tx, runID, obj, "delete",
		"deleted: unreachable at mark snapshot and unreachable/unleased at sweep recheck"); err != nil {
		return "", "", err
	}

	// Fault point BEFORE commit: row delete is pending, file already
	// quarantined. An error here rolls back the tx (row survives) — recovery
	// must restore the file. A real os.Exit here lands in the same window.
	if obj.kind == "blob" && c.cfg.Hooks.BeforeSweepCommit != nil {
		if herr := c.cfg.Hooks.BeforeSweepCommit(runID, obj.kind, obj.repo, obj.digest); herr != nil {
			_ = tx.Rollback(ctx)
			if rErr := c.fs.Restore(runID, obj.digest); rErr != nil {
				return "", "", fmt.Errorf("fault rollback restore: %w (fault: %v)", rErr, herr)
			}
			return "", "", herr
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return "", "", err
	}

	// DB delete is now durable. The quarantined bytes are removed from disk.
	if obj.kind == "blob" {
		// Fault point AFTER commit, before purge: row gone, file still in
		// quarantine — recovery must purge it.
		if c.cfg.Hooks.AfterSweepCommit != nil {
			if herr := c.cfg.Hooks.AfterSweepCommit(runID, obj.kind, obj.repo, obj.digest); herr != nil {
				return "", "", herr
			}
		}
		if err := c.fs.Purge(runID, obj.digest); err != nil {
			return "", "", fmt.Errorf("purge: %w", err)
		}
	}
	_ = c.st.AuditEvent(ctx, runID, "gc.delete", obj.kind, obj.repo, obj.digest,
		fmt.Sprintf("fence_xid=%d", fenceXID))
	return "delete", "deleted: unreachable at mark snapshot and unreachable/unleased at sweep recheck", nil
}

func (c *Collector) activeLeases(ctx context.Context, obj markObject) ([]string, error) {
	if obj.kind == "blob" {
		return c.st.ActiveLeasesForBlob(ctx, obj.digest)
	}
	return c.st.ActiveLeasesForManifest(ctx, obj.repo, obj.digest)
}

// retainReasonReachable builds a human-readable explanation of why an object
// is reachable (tag chain / lease / referenced by manifest), best effort.
func (c *Collector) retainReasonReachable(ctx context.Context, kind, repo, digest string,
	reach map[string]store.ReachRow) string {
	var roots []string
	if kind == "blob" {
		if ls, err := c.st.ActiveLeasesForBlob(ctx, digest); err == nil && len(ls) > 0 {
			roots = append(roots, fmt.Sprintf("read lease %s", strings.Join(ls, ",")))
		}
		if refs, err := c.st.BlobReferencingManifests(ctx, digest); err == nil && len(refs) > 0 {
			reachable := []string{}
			for _, r := range refs {
				if _, ok := reach[store.SetKey("manifest", r.Repo, r.Digest)]; ok {
					reachable = append(reachable, fmt.Sprintf("%s@%s", r.Repo, short(r.Digest)))
				}
			}
			if len(reachable) > 0 {
				roots = append(roots, "referenced by live manifest(s) "+strings.Join(reachable, ", "))
			}
		}
	} else {
		if ls, err := c.st.ActiveLeasesForManifest(ctx, repo, digest); err == nil && len(ls) > 0 {
			roots = append(roots, fmt.Sprintf("read lease %s", strings.Join(ls, ",")))
		}
		if tags, err := c.st.TagPointingAt(ctx, repo, digest); err == nil && len(tags) > 0 {
			roots = append(roots, fmt.Sprintf("tag(s) %s in repo %s", strings.Join(tags, ","), repo))
		}
		if refs, err := c.st.IncomingManifestRefs(ctx, repo, digest); err == nil && len(refs) > 0 {
			rs := []string{}
			for _, r := range refs {
				if _, ok := reach[store.SetKey("manifest", r.Repo, r.Digest)]; ok {
					rs = append(rs, fmt.Sprintf("%s@%s", r.Repo, short(r.Digest)))
				}
			}

			if len(rs) > 0 {
				roots = append(roots, "listed by live index/referrer "+strings.Join(rs, ", "))
			}
		}
	}
	if len(roots) == 0 {
		return "retained: reachable from a GC root at snapshot"
	}
	return "retained: " + strings.Join(roots, "; ")
}

func short(d string) string {
	if len(d) > 19 {
		return d[:19] + "..."
	}
	return d
}

func (c *Collector) auditItem(ctx context.Context, runID, kind, repo, digest, decision, reason string) {
	tx, err := c.st.Pool().Begin(ctx)
	if err != nil {
		return
	}
	defer tx.Rollback(ctx)
	_ = c.st.UpsertGCItem(ctx, tx, runID, kind, repo, digest, decision, reason)
	_ = tx.Commit(ctx)
}

func (c *Collector) auditItemTx(ctx context.Context, tx pgx.Tx, runID string, obj markObject,
	decision, reason string) error {
	return c.st.UpsertGCItem(ctx, tx, runID, obj.kind, obj.repo, obj.digest, decision, reason)
}

func listBlobsTx(ctx context.Context, tx pgx.Tx) ([]store.BlobInfo, error) {
	rows, err := tx.Query(ctx, "SELECT digest, size, created_at FROM blobs ORDER BY digest")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []store.BlobInfo
	for rows.Next() {
		var b store.BlobInfo
		if err := rows.Scan(&b.Digest, &b.Size, &b.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

func listManifestsTx(ctx context.Context, tx pgx.Tx) ([]store.AllManifest, error) {
	rows, err := tx.Query(ctx, "SELECT repo, digest FROM manifests ORDER BY repo, digest")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []store.AllManifest
	for rows.Next() {
		var m store.AllManifest
		if err := rows.Scan(&m.Repo, &m.Digest); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}
