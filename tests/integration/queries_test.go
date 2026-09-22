package integration

import (
	"testing"

	"geoterritory/internal/geometry"
	"geoterritory/internal/service"
)

// TestQueries_BBoxAndNearestAndIsolation covers bbox, Haversine nearest with
// n cap and ID tie-break, and strict org isolation.
func TestQueries_BBoxAndNearestAndIsolation(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	e.resetOrg(t, "beta")
	acme := e.orgs["acme"]
	beta := e.orgs["beta"]

	// Points on a north-south line at lng 0 for Acme.
	items := []service.BatchItem{
		{ExternalID: "A1", Lat: 10.0, Lng: 0},
		{ExternalID: "A2", Lat: 10.5, Lng: 0},
		{ExternalID: "A3", Lat: 11.0, Lng: 0},
		{ExternalID: "A4", Lat: 40.0, Lng: 0}, // far away
	}
	if _, err := e.points.BatchImport(acme, items); err != nil {
		t.Fatal(err)
	}
	// Beta's point sits right next to Acme's - it must never leak.
	if _, err := e.points.BatchImport(beta, []service.BatchItem{
		{ExternalID: "B1", Lat: 10.0001, Lng: 0},
	}); err != nil {
		t.Fatal(err)
	}

	// BBox.
	res, err := e.points.BBox(acme, geometry.BBox{MinLat: 10, MaxLat: 11, MinLng: -1, MaxLng: 1}, 100)
	if err != nil {
		t.Fatal(err)
	}
	if res.Count != 3 {
		t.Fatalf("bbox returned %d points, want 3", res.Count)
	}
	for _, p := range res.Points {
		if p.OrgID != acme {
			t.Fatal("bbox leaked another organization's point")
		}
	}

	// BBox crossing the antimeridian is rejected, not wrapped.
	if _, err := e.points.BBox(acme, geometry.BBox{MinLat: 0, MaxLat: 10, MinLng: 170, MaxLng: -170}, 10); err == nil {
		t.Fatal("antimeridian-crossing bbox must be rejected")
	}

	// Nearest: query at 10.0, expect A1 first, then A2, A3; B1 invisible.
	near, err := e.points.Nearest(acme, geometry.Vertex{Lng: 0, Lat: 10.0}, 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(near) != 4 {
		t.Fatalf("nearest returned %d, want all 4 acme points", len(near))
	}
	if near[0].ExternalID != "A1" {
		t.Fatalf("nearest[0] = %s, want A1", near[0].ExternalID)
	}
	for _, p := range near {
		if p.OrgID == beta {
			t.Fatal("nearest leaked beta point")
		}
	}
	if near[0].DistanceMeters != 0 {
		t.Fatalf("distance of co-located point = %g, want 0", near[0].DistanceMeters)
	}
	// Ordering check for A2 then A3.
	if near[1].ExternalID != "A2" || near[2].ExternalID != "A3" {
		t.Fatalf("nearest order wrong: %s, %s", near[1].ExternalID, near[2].ExternalID)
	}

	// n cap.
	if _, err := e.points.Nearest(acme, geometry.Vertex{Lng: 0, Lat: 10}, 51); err == nil {
		t.Fatal("n=51 must be rejected")
	}
	capped, err := e.points.Nearest(acme, geometry.Vertex{Lng: 0, Lat: 10}, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(capped) != 2 {
		t.Fatalf("n=2 should return 2, got %d", len(capped))
	}

	// Beta sees ONLY B1 even though A1 is equidistant/closer.
	bnear, err := e.points.Nearest(beta, geometry.Vertex{Lng: 0, Lat: 10.0}, 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(bnear) != 1 || bnear[0].ExternalID != "B1" {
		t.Fatalf("beta isolation broken: %+v", bnear)
	}
}

// TestQueries_NearestTieBreakByID inserts two points at identical coordinates
// and requires ID-ascending tie-break.
func TestQueries_NearestTieBreakByID(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "gamma")
	org := e.orgs["gamma"]
	if _, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "T1", Lat: 5, Lng: 5},
		{ExternalID: "T2", Lat: 5, Lng: 5},
		{ExternalID: "T3", Lat: 5, Lng: 5},
	}); err != nil {
		t.Fatal(err)
	}
	got, err := e.points.Nearest(org, geometry.Vertex{Lng: 5, Lat: 5}, 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("want 3 tied points, got %d", len(got))
	}
	if !(got[0].ID < got[1].ID && got[1].ID < got[2].ID) {
		t.Fatal("equal-distance tie must break by ascending id")
	}
	if got[0].DistanceMeters != 0 || got[1].DistanceMeters != 0 {
		t.Fatal("tied distances must be exactly equal")
	}
}
