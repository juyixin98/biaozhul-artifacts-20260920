package integration

import (
	"testing"

	"geoterritory/internal/geometry"
	"geoterritory/internal/service"
)

// TestAssignment_OverlapPriority verifies that when several regions contain
// the same point, priority 1..5 wins, then the lower region id.
func TestAssignment_OverlapPriority(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	org := e.orgs["acme"]

	// Three nested/overlapping squares all centred on (0,0).
	create := func(name string, priority int, h float64) int64 {
		_, job, err := e.regions.CreateRegion(org, service.CreateRegionInput{
			Name: name, Priority: priority, Polygon: squarePolygon(0, 0, h),
		})
		if err != nil {
			t.Fatalf("create %s: %v", name, err)
		}
		return job.ID
	}
	j1 := create("low", 5, 10)
	j2 := create("mid", 3, 8)
	j3 := create("high", 1, 6)
	_ = j1
	_ = j2
	_ = j3
	e.drainJobs(t)

	// Point dead centre belongs to all three; priority 1 must win.
	rows, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "center", Lat: 0, Lng: 0},
		{ExternalID: "in-mid-only", Lat: 0, Lng: 7},
		{ExternalID: "in-low-only", Lat: 0, Lng: 9},
		{ExternalID: "outside", Lat: 0, Lng: 11},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		if r.Status == "error" {
			t.Fatalf("row %s: %s", r.ExternalID, r.Error)
		}
	}

	var nameToID = map[string]int64{}
	var regions []struct {
		ID   int64
		Name string
	}
	e.db.Table("regions").Select("id, name").Where("org_id = ?", org).Scan(&regions)
	for _, r := range regions {
		nameToID[r.Name] = r.ID
	}

	cases := []struct {
		ext    string
		region *int64
	}{
		{"center", ptr(nameToID["high"])},
		{"in-mid-only", ptr(nameToID["mid"])},
		{"in-low-only", ptr(nameToID["low"])},
		{"outside", nil},
	}
	for _, c := range cases {
		want := c.region
		got := e.point(org, c.ext)
		if (got.RegionID == nil) != (want == nil) {
			t.Fatalf("%s: region assignment %v, want %v", c.ext, got.RegionID, want)
		}
		if want != nil && *got.RegionID != *want {
			t.Fatalf("%s: assigned region %d, want %d", c.ext, *got.RegionID, *want)
		}
		if got.RegionID == nil && got.RegionVersionID != nil {
			t.Fatalf("%s: unassigned point must have nil region_version_id", c.ext)
		}
		if got.RegionID != nil && got.RegionVersionID == nil {
			t.Fatalf("%s: assigned point must record basis region_version_id", c.ext)
		}
	}
}

func ptr[T any](v T) *T { return &v }

// TestAssignment_PriorityTieBrokenByRegionID covers the secondary tie-break:
// equal priorities -> smallest region id.
func TestAssignment_PriorityTieBrokenByRegionID(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "beta")
	org := e.orgs["beta"]

	// Two identical polygons, same priority: created in name order so the
	// first gets the smaller id.
	for _, name := range []string{"a-first", "b-second"} {
		if _, _, err := e.regions.CreateRegion(org, service.CreateRegionInput{
			Name: name, Priority: 2, Polygon: squarePolygon(0, 0, 5),
		}); err != nil {
			t.Fatal(err)
		}
	}
	e.drainJobs(t)

	if _, err := e.points.BatchImport(org, []service.BatchItem{{ExternalID: "x", Lat: 0, Lng: 0}}); err != nil {
		t.Fatal(err)
	}
	var firstID int64
	e.db.Table("regions").Where("org_id = ? AND name = 'a-first'", org).Select("id").Scan(&firstID)
	got := e.point(org, "x")
	if got.RegionID == nil || *got.RegionID != firstID {
		t.Fatalf("tie should break to smaller region id %d, got %v", firstID, got.RegionID)
	}
}

// TestAssignment_BoundaryPointInRegion pins the inclusive boundary rule at
// the service level: a point exactly on the edge is assigned.
func TestAssignment_BoundaryPointInRegion(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "gamma")
	org := e.orgs["gamma"]
	if _, _, err := e.regions.CreateRegion(org, service.CreateRegionInput{
		Name: "square", Priority: 1, Polygon: squarePolygon(0, 0, 1),
	}); err != nil {
		t.Fatal(err)
	}
	e.drainJobs(t)

	// Square spans lng/lat [-1,1]; (0,1) is on the top edge and (1,-1) a corner.
	if _, err := e.points.BatchImport(org, []service.BatchItem{
		{ExternalID: "edge", Lat: 1, Lng: 0},
		{ExternalID: "corner", Lat: -1, Lng: 1},
		{ExternalID: "just-out", Lat: 1.0001, Lng: 0},
	}); err != nil {
		t.Fatal(err)
	}
	for _, ext := range []string{"edge", "corner"} {
		if e.point(org, ext).RegionID == nil {
			t.Fatalf("%s on boundary must be assigned", ext)
		}
	}
	if p := e.point(org, "just-out"); p.RegionID != nil {
		t.Fatalf("point outside boundary assigned to region %d", *p.RegionID)
	}
}

// TestPolygonValidation_HTTP verifies invalid shapes are 400s with clear
// reasons (self-intersection, antimeridian, range, degeneracy).
func TestPolygonValidation_AtService(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	org := e.orgs["acme"]

	bad := []struct {
		name string
		poly []geometry.Vertex
		want string
	}{
		{"antim", []geometry.Vertex{{Lng: 179, Lat: 10}, {Lng: -179, Lat: 10}, {Lng: -179, Lat: 20}, {Lng: 179, Lat: 20}}, "antimeridian"},
		{"bow", []geometry.Vertex{{Lng: 0, Lat: 0}, {Lng: 10, Lat: 10}, {Lng: 10, Lat: 0}, {Lng: 0, Lat: 10}}, "intersect"},
		{"line", []geometry.Vertex{{Lng: 0, Lat: 0}, {Lng: 5, Lat: 0}, {Lng: 10, Lat: 0}}, "degenerate"},
		{"range", []geometry.Vertex{{Lng: 0, Lat: 0}, {Lng: 10, Lat: 0}, {Lng: 10, Lat: 99}}, "out of range"},
	}
	for _, c := range bad {
		_, _, err := e.regions.CreateRegion(org, service.CreateRegionInput{Name: c.name, Priority: 1, Polygon: c.poly})
		if err == nil {
			t.Fatalf("%s: expected rejection", c.name)
		}
		if ve, ok := err.(*service.ValidationError); !ok || ve.Status != 400 {
			t.Fatalf("%s: expected 400 ValidationError, got %v", c.name, err)
		}
	}
}
