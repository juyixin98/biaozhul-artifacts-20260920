package integration

import (
	"fmt"
	"testing"

	"geoterritory/internal/service"
)

// TestBatch_IdempotentAndPartialErrors covers:
//   - create, then replay identical rows -> unchanged, no version bump;
//   - coordinate change without expected_version -> row error, others kept;
//   - coordinate change WITH expected_version -> updated, version incremented;
//   - per-row validation errors don't lose successful rows;
//   - batch > 1000 rejected wholesale.
func TestBatch_IdempotentAndPartialErrors(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	org := e.orgs["acme"]

	first, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "P1", Lat: 1, Lng: 1},
		{ExternalID: "P2", Lat: 2, Lng: 2},
	})
	if err != nil {
		t.Fatal(err)
	}
	if first[0].Status != "created" || first[1].Status != "created" {
		t.Fatalf("expected created/created, got %s/%s", first[0].Status, first[1].Status)
	}
	if e.point(org, "P1").Version != 1 {
		t.Fatal("new point version should be 1")
	}

	// Replay identical: idempotent, version unchanged.
	replay, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "P1", Lat: 1, Lng: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	if replay[0].Status != "unchanged" || e.point(org, "P1").Version != 1 {
		t.Fatal("identical replay must be unchanged at version 1")
	}

	// Move without token: row error.
	noTok, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "P1", Lat: 1.5, Lng: 1.5},
		{ExternalID: "P2", Lat: 2, Lng: 2},
	})
	if err != nil {
		t.Fatal(err)
	}
	if noTok[0].Status != "error" || noTok[0].ErrorCode != "VERSION_CONFLICT" {
		t.Fatalf("move without token must be VERSION_CONFLICT, got %+v", noTok[0])
	}
	if noTok[1].Status != "unchanged" {
		t.Fatalf("valid sibling row must be kept, got %s", noTok[1].Status)
	}

	// Move with a stale token: rejected, point stays at v1.
	stale := int64(99)
	wrongTok, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "P1", Lat: 1.6, Lng: 1.6, ExpectedVersion: &stale},
	})
	if err != nil {
		t.Fatal(err)
	}
	if wrongTok[0].Status != "error" || wrongTok[0].ErrorCode != "VERSION_CONFLICT" {
		t.Fatalf("stale token must be rejected, got %+v", wrongTok[0])
	}
	if e.point(org, "P1").Version != 1 || e.point(org, "P1").Lat != 1 {
		t.Fatal("rejected overwrite must not change the row")
	}

	// Move with the correct token: applied, version 2.
	v1 := int64(1)
	ok, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "P1", Lat: 1.6, Lng: 1.6, ExpectedVersion: &v1},
	})
	if err != nil {
		t.Fatal(err)
	}
	if ok[0].Status != "updated" || e.point(org, "P1").Version != 2 {
		t.Fatalf("expected updated@2, got %s @ %d", ok[0].Status, e.point(org, "P1").Version)
	}

	// Invalid + valid in one batch: success row retained.
	mix, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "BAD", Lat: 200, Lng: 0},
		{ExternalID: "P3", Lat: 3, Lng: 3},
	})
	if err != nil {
		t.Fatal(err)
	}
	if mix[0].ErrorCode != "INVALID_COORDINATES" {
		t.Fatalf("row1 must be INVALID_COORDINATES, got %+v", mix[0])
	}
	if mix[1].Status != "created" {
		t.Fatalf("row2 must be created, got %s", mix[1].Status)
	}
	if e.point(org, "P3").ID == 0 {
		t.Fatal("successful row must be persisted despite sibling failure")
	}
}

// TestBatch_Limit rejects a batch larger than 1000.
func TestBatch_Limit(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "beta")
	org := e.orgs["beta"]
	items := make([]service.BatchItem, 1001)
	for i := range items {
		items[i] = service.BatchItem{ExternalID: fmt.Sprintf("X%d", i), Lat: 1, Lng: 1}
	}
	if _, err := e.points.BatchImport(org, items); err == nil {
		t.Fatal("batch of 1001 must be rejected")
	}
}

// TestPointUpdate_ConcurrentNoLostUpdate fires parallel coordinate updates
// with optimistic tokens; exactly one wins per version, losers see 409 and
// can retry - the final state is always a fully applied update, never a
// silently clobbered one.
func TestPointUpdate_ConcurrentNoLostUpdate(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "gamma")
	org := e.orgs["gamma"]
	if _, err := e.points.BatchImport(org, []service.BatchItem{{ExternalID: "MOV", Lat: 0, Lng: 0}}); err != nil {
		t.Fatal(err)
	}

	const writers = 8
	type outcome struct {
		ok      bool
		version int64
	}
	resCh := make(chan outcome, writers)
	for i := 0; i < writers; i++ {
		i := i
		go func() {
			// Every writer starts believing version is 1.
			_, _, err := e.points.UpdatePoint(org, "MOV", float64(i+1), float64(i+1), 1)
			if err == nil {
				resCh <- outcome{ok: true, version: 2}
				return
			}
			resCh <- outcome{ok: false}
		}()
	}
	wins, losses := 0, 0
	for i := 0; i < writers; i++ {
		o := <-resCh
		if o.ok {
			wins++
		} else {
			losses++
		}
	}
	if wins != 1 || losses != writers-1 {
		t.Fatalf("expected exactly 1 winner and %d conflicts, got wins=%d losses=%d", writers-1, wins, losses)
	}
	if v := e.point(org, "MOV").Version; v != 2 {
		t.Fatalf("surviving version must be 2, got %d", v)
	}

	// A loser retries with the new token and succeeds -> no update is lost
	// from the application's perspective.
	p, _, err := e.points.UpdatePoint(org, "MOV", 42, 42, 2)
	if err != nil {
		t.Fatalf("retry after conflict failed: %v", err)
	}
	if p.Version != 3 || p.Lat != 42 {
		t.Fatalf("retry should produce v3 @ 42, got v%d @ %g", p.Version, p.Lat)
	}
}
