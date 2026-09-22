package integration

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"testing"
	"time"

	"gorm.io/gorm"

	"geoterritory/internal/engine"
	"geoterritory/internal/models"
	"geoterritory/internal/store"
)

func publishAndWait(t *testing.T, key string, region map[string]any) (catalogVersion int) {
	t.Helper()
	st, body := doJSON(t, http.MethodPost, "/v1/regions", key, region)
	if st != http.StatusAccepted {
		t.Fatalf("publish: %d %v", st, body)
	}
	job := waitForJob(t, key, body["job_id"].(float64))
	if job["status"] != models.JobDone {
		t.Fatalf("job not done: %v", job)
	}
	return int(body["catalog_version"].(float64))
}

// A region republish moves points between regions; old catalog versions keep
// returning the old assignment and the flip is atomic.
func TestReassignmentAndOldVersionConsistency(t *testing.T) {
	key := newOrg(t)
	suffix := uniq()
	// Publish one big square (priority 1) covering the point.
	v1 := publishAndWait(t, key, squareRegion("rep-zone-"+suffix, 1, 0, 0, 5))
	if v1 != 1 {
		t.Fatalf("first catalog version in a fresh org = %d want 1", v1)
	}
	ext := "rep-pt-" + suffix
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", key, map[string]any{
		"points": []map[string]any{{"external_id": ext, "lat": 0, "lng": 0}},
	}); st != http.StatusOK || body["failed"].(float64) != 0 {
		t.Fatalf("seed point: %v", body)
	}
	st, before := doJSON(t, http.MethodGet, "/v1/points/"+ext+"?catalog_version=current", key, nil)
	if st != http.StatusOK {
		t.Fatalf("get v1: %v", before)
	}
	zoneV1 := before["point"].(map[string]any)["region_id"].(float64)
	if zoneV1 == 0 {
		t.Fatal("point should be assigned under v1")
	}

	// Republish the SAME name with a polygon that no longer covers the origin
	// (shift far away). The point must become unassigned under v2 but STAY
	// assigned under v1.
	shifted := squareRegion("rep-zone-"+suffix, 1, 60, 60, 1)
	stPub, pubBody := doJSON(t, http.MethodPost, "/v1/regions", key, shifted)
	if stPub != http.StatusAccepted {
		t.Fatalf("republish: %d %v", stPub, pubBody)
	}
	// Reading explicit old version must always show the old assignment,
	// regardless of whether the worker has already flipped.
	st, old := doJSON(t, http.MethodGet, "/v1/points/"+ext+"?catalog_version="+itoa(v1), key, nil)
	if st != http.StatusOK {
		t.Fatalf("old version read: %d %v", st, old)
	}
	if old["point"].(map[string]any)["region_id"].(float64) != zoneV1 {
		t.Fatalf("old version assignment changed mid-recompute: %v", old)
	}

	waitForJob(t, key, pubBody["job_id"].(float64))
	st, after := doJSON(t, http.MethodGet, "/v1/points/"+ext+"?catalog_version=current", key, nil)
	if after["point"].(map[string]any)["region_id"].(float64) != 0 {
		t.Fatalf("after flip point must be unassigned under v2: %v", after)
	}
	// Historical read still consistent.
	st, oldAgain := doJSON(t, http.MethodGet, "/v1/points/"+ext+"?catalog_version="+itoa(v1), key, nil)
	if oldAgain["point"].(map[string]any)["region_id"].(float64) != zoneV1 {
		t.Fatalf("historical v1 read must remain assigned: %v", oldAgain)
	}
}

// Points imported or moved DURING a recompute cannot be missed: the writer
// fills the not-yet-current catalog and the worker does a locked delta sweep.
func TestPointsArrivingDuringRecomputeNotMissed(t *testing.T) {
	suffix := uniq()
	// Large square so all test points are inside.
	publishAndWait(t, env.keyA, squareRegion("recompute-zone-"+suffix, 1, 0, 0, 10))

	// Seed one point.
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, map[string]any{
		"points": []map[string]any{{"external_id": "base-" + suffix, "lat": 0, "lng": 0}},
	}); st != http.StatusOK {
		t.Fatalf("seed: %v", body)
	}

	// Publish a new catalog (tiny polygon shift) to create an active job, but
	// do NOT let the worker finish yet. Use a big half-square still covering
	// everything we import.
	st, body := doJSON(t, http.MethodPost, "/v1/regions", env.keyA,
		squareRegion("recompute-zone-"+suffix, 2, 0, 0, 9))
	if st != http.StatusAccepted {
		t.Fatalf("publish: %d %v", st, body)
	}
	jobID := body["job_id"].(float64)

	// Immediately import more points and move an existing one while the
	// recompute is pending (worker tick is 200ms; fire right away).
	for i := 0; i < 25; i++ {
		lat := float64(i%5) * 0.1
		lng := float64(i/5) * 0.1
		if st, b := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, map[string]any{
			"points": []map[string]any{{
				"external_id": "mid-" + suffix + "-" + itoa(i), "lat": lat, "lng": lng,
			}},
		}); st != http.StatusOK || b["failed"].(float64) != 0 {
			t.Fatalf("mid import %d: %v %v", i, st, b)
		}
	}

	waitForJob(t, env.keyA, jobID)

	// Every arriving point must be assigned under the NEW catalog (the square
	// still covers them; region_id > 0), proving none was missed.
	for i := 0; i < 25; i++ {
		st, got := doJSON(t, http.MethodGet,
			"/v1/points/mid-"+suffix+"-"+itoa(i)+"?catalog_version=current", env.keyA, nil)
		if st != http.StatusOK {
			t.Fatalf("point %d missing after recompute: %v", i, got)
		}
		if got["point"].(map[string]any)["region_id"].(float64) == 0 {
			t.Fatalf("arriving point %d must be assigned under new catalog", i)
		}
	}
}

// Crash recovery: kill a worker mid-job; after the heartbeat goes stale a new
// worker claims it and the job completes, and the catalog flips exactly once.
func TestCrashRecovery(t *testing.T) {
	suffix := uniq()
	publishAndWait(t, env.keyA, squareRegion("crash-zone-"+suffix, 1, 0, 0, 5))

	// Seed points.
	pts := make([]map[string]any, 0, 60)
	for i := 0; i < 60; i++ {
		pts = append(pts, map[string]any{
			"external_id": "crash-" + suffix + "-" + itoa(i),
			"lat":         float64(i%7) * 0.2,
			"lng":         float64(i/7) * 0.2,
		})
	}
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA,
		map[string]any{"points": pts}); st != http.StatusOK || body["failed"].(float64) != 0 {
		t.Fatalf("seed batch: %v", body)
	}

	// Publish new version, claim the job manually once (simulating a worker
	// that crashes after claim + one partial batch) using store primitives.
	st, pub := doJSON(t, http.MethodPost, "/v1/regions", env.keyA,
		squareRegion("crash-zone-"+suffix, 1, 0, 0, 4))
	if st != http.StatusAccepted {
		t.Fatalf("publish: %v", pub)
	}
	jobID := uint64(pub["job_id"].(float64))

	// Simulate crashed worker: claim then write one batch then die (no
	// heartbeat refresh, no finalize).
	job, err := store.ClaimReassignJob(env.db, 10*time.Millisecond)
	if err != nil || job == nil || job.ID != jobID {
		t.Fatalf("manual claim: job=%v err=%v want id %d", job, err, jobID)
	}
	cat, err := store.LoadCatalog(env.db, job.OrgID, job.ToVersion)
	if err != nil {
		t.Fatal(err)
	}
	batch, err := store.MissingPointBatch(env.db, job.OrgID, job.ToVersion, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(batch) == 0 {
		t.Fatal("expected a missing-point batch")
	}
	if err := store.BulkInsertAssignments(env.db, job.OrgID, cat, batch); err != nil {
		t.Fatal(err)
	}
	// "Crash": no further heartbeat. Wait for staleness (worker uses 2s).

	// The normal worker must reclaim after the heartbeat is stale and finish.
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(300 * time.Millisecond)
		env.worker.RunOnce(context.Background())
		cur, err := store.GetJob(env.db, job.OrgID, jobID)
		if err != nil {
			t.Fatal(err)
		}
		if cur.Status == models.JobDone {
			break
		}
		if cur.Status == models.JobFailed {
			t.Fatalf("reclaimed job failed: %s", cur.Error)
		}
	}
	cur, err := store.GetJob(env.db, job.OrgID, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if cur.Status != models.JobDone {
		t.Fatalf("job not recovered to DONE: %s", cur.Status)
	}
	stRow, err := store.CatalogStateRow(env.db, job.OrgID)
	if err != nil {
		t.Fatal(err)
	}
	if stRow.CurrentVersion != job.ToVersion {
		t.Fatalf("current=%d want %d after recovery", stRow.CurrentVersion, job.ToVersion)
	}
}

// A stale/late finalize must never flip the catalog backward or overwrite a
// newer result. This drives FinalizeJob directly with a job whose target is no
// longer published.
func TestStaleFinalizeCannotOverwrite(t *testing.T) {
	orgID := orgIDByKey(t, env.keyB)
	suffix := uniq()
	// Publish twice in org-b via the API, waiting for the first job.
	publishAndWait(t, env.keyB, squareRegion("stale-1-"+suffix, 1, 0, 0, 5))
	st, second := doJSON(t, http.MethodPost, "/v1/regions", env.keyB,
		squareRegion("stale-1-"+suffix, 1, 0, 0, 4))
	if st != http.StatusAccepted {
		t.Fatalf("second publish: %v", second)
	}
	waitForJob(t, env.keyB, second["job_id"].(float64))

	before, err := store.CatalogStateRow(env.db, orgID)
	if err != nil {
		t.Fatal(err)
	}
	// Fabricate a late job row at RUNNING whose target (version 1) is older
	// than the currently published catalog. The generated-column unique key
	// only allows ONE active job per org; since the real jobs are DONE (NULL
	// active_key), inserting this fake active row is allowed.
	fake := &models.ReassignJob{
		OrgID:       orgID,
		FromVersion: 0,
		ToVersion:   1, // older than the published/current catalog
		Status:      models.JobRunning,
	}
	if err := env.db.Create(fake).Error; err != nil {
		t.Fatalf("create fake stale job: %v", err)
	}
	err = store.WithOrgLock(env.db, orgID, 5, func(tx *gorm.DB) error {
		return store.FinalizeJob(tx,
			func(version int) (*engine.Catalog, error) {
				return store.LoadCatalog(tx, orgID, version)
			}, fake, 50)
	})
	if !errors.Is(err, store.ErrJobStale) {
		t.Fatalf("stale finalize must return ErrJobStale, got %v", err)
	}
	after, err := store.CatalogStateRow(env.db, orgID)
	if err != nil {
		t.Fatal(err)
	}
	if after.CurrentVersion != before.CurrentVersion {
		t.Fatalf("stale job moved current pointer: %d -> %d", before.CurrentVersion, after.CurrentVersion)
	}
}

func itoa(i int) string {
	return strconv.Itoa(i)
}
