package integration

import (
	"testing"
	"time"

	"geoterritory/internal/geometry"
	"geoterritory/internal/jobs"
	"geoterritory/internal/models"
	"geoterritory/internal/service"
)

// TestRecompute_AtomicSwitchAndOldVersionRead verifies:
//  1. while a job is pending/running, existing points still read with the
//     OLD basis version and old seq (old-version queries stay consistent);
//  2. after the worker drains, every point carries the new basis and seq
//     atomically;
//  3. a version history is retained and 'building' rows never leak.
func TestRecompute_AtomicSwitchAndOldVersionRead(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	org := e.orgs["acme"]

	// v1 square [-2,2], publish.
	_, j1, err := e.regions.CreateRegion(org, service.CreateRegionInput{
		Name: "box", Priority: 1, Polygon: squarePolygon(0, 0, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	e.drainJobs(t)
	if e.activeSeq(org) != 1 {
		t.Fatalf("seq after first publish = %d, want 1", e.activeSeq(org))
	}

	rows, _ := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "stays", Lat: 0, Lng: 0},    // inside v1 and v2
		{ExternalID: "leaves", Lat: 1.9, Lng: 0}, // inside v1, outside v2
		{ExternalID: "joins", Lat: 2.5, Lng: 0},  // outside v1, inside v3
	})
	for _, r := range rows {
		if r.Status == "error" {
			t.Fatalf("seed: %s %s", r.ExternalID, r.Error)
		}
	}
	var v1ID int64
	e.db.Table("region_versions").Where("region_id = (SELECT id FROM regions WHERE name='box') AND version = 1").Select("id").Scan(&v1ID)
	if p := e.point(org, "leaves"); p.RegionVersionID == nil || *p.RegionVersionID != v1ID {
		t.Fatalf("leaves should be assigned under v1 id %d, got %v", v1ID, p.RegionVersionID)
	}

	// Publish v2: square [-3,3] for joins, but leaves at 1.9 stays inside...
	// adjust: v2 square half-size 2.2 so leaves(1.9) in, joins(2.5) in.
	// Instead use v2 half-size 3 and move leaves OUT by shrinking elsewhere:
	// simpler - v2 = half 3 box (covers joins), leaves remains inside (1.9<3).
	// To exercise a point leaving, publish v3 half-size 1 after.
	_, _, err = e.regions.PublishVersion(org, regionID(t, e, org, "box"), service.CreateRegionInput{
		Priority: 1, Polygon: squarePolygon(0, 0, 3),
	})
	if err != nil {
		t.Fatal(err)
	}
	// Before draining, all points must still report seq 1 / old basis.
	time.Sleep(100 * time.Millisecond) // let worker not run (it isn't started in this test)
	if p := e.point(org, "joins"); p.RegionID != nil {
		t.Fatalf("before switch, joins must stay unassigned under old set, got region %v", p.RegionID)
	}
	if p := e.point(org, "stays"); p.AssignSetSeq != 1 {
		t.Fatalf("before switch, assign_set_seq must stay 1, got %d", p.AssignSetSeq)
	}
	// building versions must not be visible via the effective-set loader.
	set, err := service.LoadEffectiveSet(e.db, org)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range set.Shapes {
		var rv models.RegionVersion
		e.db.First(&rv, s.RegionVersionID)
		if rv.Status != "active" {
			t.Fatal("effective set leaked a non-active version")
		}
	}

	e.drainJobs(t)
	if e.activeSeq(org) != 2 {
		t.Fatalf("seq after v2 = %d, want 2", e.activeSeq(org))
	}
	if p := e.point(org, "joins"); p.RegionID == nil {
		t.Fatal("after switch joins must be assigned")
	}
	if p := e.point(org, "joins"); p.AssignSetSeq != 2 {
		t.Fatalf("joins assign_set_seq %d, want 2", p.AssignSetSeq)
	}

	// v3 shrink: leaves(1.9) now outside, joins(2.5) outside, stays inside.
	_, _, err = e.regions.PublishVersion(org, regionID(t, e, org, "box"), service.CreateRegionInput{
		Priority: 1, Polygon: squarePolygon(0, 0, 1),
	})
	if err != nil {
		t.Fatal(err)
	}
	e.drainJobs(t)
	if p := e.point(org, "leaves"); p.RegionID != nil {
		t.Fatalf("leaves must be unassigned under v3, got %v", *p.RegionID)
	}
	if p := e.point(org, "joins"); p.RegionID != nil {
		t.Fatalf("joins must be unassigned under v3, got %v", *p.RegionID)
	}
	if p := e.point(org, "stays"); p.RegionID == nil {
		t.Fatal("stays must remain assigned")
	}
	// All points stamped with seq 3 in the same switch.
	for _, ext := range []string{"stays", "leaves", "joins"} {
		if seq := e.point(org, ext).AssignSetSeq; seq != 3 {
			t.Fatalf("%s seq %d, want 3", ext, seq)
		}
	}
	// Version history retained.
	var vcount int64
	e.db.Model(&models.RegionVersion{}).Where("region_id = (SELECT id FROM regions WHERE name='box')").Count(&vcount)
	if vcount != 3 {
		t.Fatalf("expected 3 immutable versions retained, got %d", vcount)
	}
	_ = j1
}

// TestRecompute_PointMovedDuringStagingIsNotMissed is the core race, driven
// deterministically through the worker's split STAGE/APPLY entry points:
//
//	STAGE  -> mutate the world (move a point, insert a new one) -> APPLY
//
// Staged rows carry the point version they saw; apply must detect the changed
// version, recompute against the target set live, and never miss the insert.
func TestRecompute_PointMovedDuringStagingIsNotMissed(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "beta")
	org := e.orgs["beta"]

	// v1: a box in the west half.
	_, _, err := e.regions.CreateRegion(org, service.CreateRegionInput{
		Name: "west", Priority: 1, Polygon: squarePolygon(-5, 0, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	e.drainJobs(t)
	if _, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "moose", Lat: 0, Lng: -5}, // inside west v1
		{ExternalID: "still", Lat: 0, Lng: -9}, // inside west v1, never moves
	}); err != nil {
		t.Fatal(err)
	}

	// Publish v2 moving the box to the EAST half.
	_, j2, err := e.regions.PublishVersion(org, regionID(t, e, org, "west"), service.CreateRegionInput{
		Priority: 1, Polygon: squarePolygon(5, 0, 2),
	})
	if err != nil {
		t.Fatal(err)
	}

	runner := jobs.NewRunner(e.db)
	id, err := runner.ClaimOne()
	if err != nil || id != j2.ID {
		t.Fatalf("claim: id=%d want %d err=%v", id, j2.ID, err)
	}
	if err := runner.StageJob(id); err != nil {
		t.Fatalf("stage: %v", err)
	}

	// The world mutates AFTER staging: moose moves into the new east box,
	// and a brand new point appears there.
	if _, _, err := e.points.UpdatePoint(org, "moose", 0, 5, 1); err != nil {
		t.Fatalf("move moose: %v", err)
	}
	if _, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "newkid", Lat: 0, Lng: 5},
	}); err != nil {
		t.Fatal(err)
	}

	// Before apply, live rows must still describe the OLD set (seq 1, west
	// box): moose at lng=5 is outside it and therefore unassigned even after
	// its coordinate write, and the seq has not advanced.
	if p := e.point(org, "moose"); p.AssignSetSeq != 1 || p.RegionID != nil {
		t.Fatalf("pre-apply state leaked: %+v", p)
	}
	if e.activeSeq(org) != 1 {
		t.Fatal("active_set_seq must not move until the switch commits")
	}

	if err := runner.ApplyJob(id); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if e.activeSeq(org) != 2 {
		t.Fatalf("seq after switch = %d, want 2", e.activeSeq(org))
	}

	// moose was staged as an outsider at its OLD coordinates/version; the
	// version mismatch forces live recompute, so it ends assigned in the box
	// it moved into.
	p := e.point(org, "moose")
	if p.RegionID == nil {
		t.Fatal("moose moved during recompute but staged (stale) result was applied: update missed")
	}
	if p.AssignSetSeq != 2 {
		t.Fatalf("moose seq %d, want 2", p.AssignSetSeq)
	}
	// newkid did not exist when staging ran; it must still be classified.
	nk := e.point(org, "newkid")
	if nk.ID == 0 || nk.RegionID == nil {
		t.Fatal("point inserted between stage and apply was missed")
	}
	if nk.AssignSetSeq != 2 {
		t.Fatalf("newkid seq %d, want 2", nk.AssignSetSeq)
	}
	// still @ lng=-9 never moved: staged result applies directly, unassigned.
	if sp := e.point(org, "still"); sp.RegionID != nil || sp.AssignSetSeq != 2 {
		t.Fatalf("still got %+v, want unassigned @ seq 2", sp)
	}

	// No leftover staging rows after a clean apply.
	var leftover int64
	e.db.Model(&models.AssignmentStaging{}).Where("job_id = ?", id).Count(&leftover)
	if leftover != 0 {
		t.Fatalf("%d staging rows left after apply", leftover)
	}

	// And after a later switch moves the box back west, moose (still at
	// lng=5) must follow the new set and become unassigned.
	_, j3, err := e.regions.PublishVersion(org, regionID(t, e, org, "west"), service.CreateRegionInput{
		Priority: 1, Polygon: squarePolygon(-5, 0, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = j3
	e.drainJobs(t)
	if p := e.point(org, "moose"); p.RegionID != nil || p.AssignSetSeq != 3 {
		t.Fatalf("moose should be unassigned @ seq 3 after box moved west, got %+v", p)
	}
	if nk := e.point(org, "newkid"); nk.RegionID != nil {
		t.Fatal("newkid should also be unassigned after box moved west")
	}
}

// TestRecompute_LateJobCannotOverwrite enqueues two publishes, manually marks
// the first job superseded, and proves its (stale) results never land on top
// of the newer set via the target_seq guard.
func TestRecompute_LateJobCannotOverwrite(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "gamma")
	org := e.orgs["gamma"]

	_, j1, err := e.regions.CreateRegion(org, service.CreateRegionInput{
		Name: "r", Priority: 1, Polygon: squarePolygon(0, 0, 1),
	})
	if err != nil {
		t.Fatal(err)
	}
	_, j2, err := e.regions.PublishVersion(org, regionID(t, e, org, "r"), service.CreateRegionInput{
		Priority: 1, Polygon: squarePolygon(0, 0, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	// Drain both normally: seq must end at 2.
	e.drainJobs(t)
	if e.activeSeq(org) != 2 {
		t.Fatalf("seq = %d, want 2", e.activeSeq(org))
	}

	// Now attempt to re-apply job1 directly through the pipeline: the guard
	// must turn it 'superseded' instead of rewinding seq to 1. We simulate a
	// late commit by resetting the job row and re-running a runner cycle.
	e.db.Model(&models.ReassignJob{}).Where("id = ?", j1.ID).
		Updates(map[string]any{"status": "pending", "error": "", "finished_at": nil})

	if _, err := e.points.BatchImport(org, []service.BatchItem{{ExternalID: "z", Lat: 0, Lng: 1.5}}); err != nil {
		t.Fatal(err)
	}
	e.drainJobs(t)
	if e.activeSeq(org) != 2 {
		t.Fatalf("late job rewound seq to %d, guard failed", e.activeSeq(org))
	}
	// z @ 1.5 is inside v2 (half 2) but outside v1 (half 1); it must remain
	// assigned, proving stale v1 results did not overwrite.
	if p := e.point(org, "z"); p.RegionID == nil {
		t.Fatal("late job overwrote new assignment with stale result")
	}
	if j1.ID <= 0 || j2.ID <= j1.ID {
		t.Fatal("job ids should be monotonic")
	}
}

// TestRecompute_CrashRecovery kills a runner mid-pipeline (jobs left
// 'running', possibly with partial staging), then starts a new one. All jobs
// must still reach 'done' and the final assignments must be correct.
func TestRecompute_CrashRecovery(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	org := e.orgs["acme"]

	_, j1, err := e.regions.CreateRegion(org, service.CreateRegionInput{
		Name: "crashbox", Priority: 1, Polygon: squarePolygon(0, 0, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	// Simulate a crash AFTER claim: job stuck 'running' with partial staging.
	e.db.Model(&models.ReassignJob{}).Where("id = ?", j1.ID).
		Update("status", "running")
	e.db.Exec(`DELETE FROM assignment_staging WHERE job_id = ?`, j1.ID)
	e.db.Exec(`INSERT INTO assignment_staging (job_id, point_id, org_id, region_id, region_version_id, point_version)
		VALUES (?, 999999, ?, NULL, NULL, 1)`, j1.ID, org) // orphaned partial row

	if _, err := e.points.BatchImport(org, []service.BatchItem{{ExternalID: "c1", Lat: 0, Lng: 0}}); err != nil {
		t.Fatal(err)
	}

	// Fresh runner adopts the stuck job.
	e.drainJobs(t)
	if e.activeSeq(org) != 1 {
		t.Fatalf("after recovery seq = %d, want 1", e.activeSeq(org))
	}
	if p := e.point(org, "c1"); p.RegionID == nil {
		t.Fatal("after recovery c1 should be assigned")
	}
	var job models.ReassignJob
	e.db.First(&job, j1.ID)
	if job.Status != "done" {
		t.Fatalf("adopted job ended %s, want done", job.Status)
	}
	var leftover int64
	e.db.Model(&models.AssignmentStaging{}).Where("job_id = ?", j1.ID).Count(&leftover)
	if leftover != 0 {
		t.Fatalf("staging rows should be cleaned after apply, %d remain", leftover)
	}
}

func regionID(t *testing.T, e *env, orgID int64, name string) int64 {
	t.Helper()
	var id int64
	if err := e.db.Table("regions").Where("org_id = ? AND name = ?", orgID, name).Select("id").Scan(&id).Error; err != nil {
		t.Fatal(err)
	}
	if id == 0 {
		t.Fatalf("region %s not found", name)
	}
	return id
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		b[pos] = '-'
	}
	return string(b[pos:])
}

var _ = geometry.Vertex{}
