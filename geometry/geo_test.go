package geometry

import (
	"math"
	"testing"
)

func square(cx, cy, half float64) []LatLng {
	return []LatLng{
		{Lat: cy - half, Lng: cx - half},
		{Lat: cy + half, Lng: cx - half},
		{Lat: cy + half, Lng: cx + half},
		{Lat: cy - half, Lng: cx + half},
	}
}

func TestValidateRing_OK(t *testing.T) {
	if err := ValidateRing(square(0, 0, 1)); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if err := ValidateRing([]LatLng{
		{Lat: 0, Lng: 0}, {Lat: 1, Lng: 0}, {Lat: 0, Lng: 1},
	}); err != nil {
		t.Fatalf("triangle should be valid: %v", err)
	}
}

func TestValidateRing_TooFewOrDuplicate(t *testing.T) {
	if err := ValidateRing([]LatLng{{0, 0}, {1, 1}}); err == nil {
		t.Fatal("2-vertex ring must be rejected")
	}
	if err := ValidateRing([]LatLng{
		{Lat: 0, Lng: 0}, {Lat: 1, Lng: 0}, {Lat: 1, Lng: 1}, {Lat: 0, Lng: 0},
	}); err == nil {
		t.Fatal("repeated closing vertex must be rejected")
	}
	// Collinear triangle -> zero area.
	if err := ValidateRing([]LatLng{
		{Lat: 0, Lng: 0}, {Lat: 1, Lng: 0}, {Lat: 2, Lng: 0},
	}); err == nil {
		t.Fatal("collinear ring must be rejected")
	}
	// Spike: a vertex on the bottom edge dents inward and back out. Non-zero
	// shoelace area, but the vertex lies on a non-incident edge.
	spike := []LatLng{
		{Lat: 0, Lng: 0}, {Lat: 4, Lng: 0}, {Lat: 2, Lng: 0},
		{Lat: 4, Lng: 4}, {Lat: 0, Lng: 4},
	}
	if err := ValidateRing(spike); err == nil {
		t.Fatal("spiked polygon (vertex lying on a non-incident edge) must be rejected")
	}
}

func TestValidateRing_SelfIntersecting(t *testing.T) {
	bowtie := []LatLng{
		{Lat: 0, Lng: 0},
		{Lat: 2, Lng: 2},
		{Lat: 0, Lng: 2},
		{Lat: 2, Lng: 0},
	}
	if err := ValidateRing(bowtie); err == nil {
		t.Fatal("bow-tie polygon must be rejected as self-intersecting")
	}
}

func TestValidateRing_CoordinateRange(t *testing.T) {
	bad := []LatLng{
		{Lat: 0, Lng: 0}, {Lat: 1, Lng: 0}, {Lat: 0, Lng: 200},
	}
	if err := ValidateRing(bad); err == nil {
		t.Fatal("longitude 200 must be rejected")
	}
	bad2 := []LatLng{
		{Lat: -91, Lng: 0}, {Lat: 1, Lng: 0}, {Lat: 0, Lng: 1},
	}
	if err := ValidateRing(bad2); err == nil {
		t.Fatal("latitude -91 must be rejected")
	}
}

func TestValidateRing_Antimeridian(t *testing.T) {
	// Polygon whose edge crosses the dateline: 179 -> -179. Must be REJECTED,
	// never silently treated as a tiny polygon in lat-lon space.
	cross := []LatLng{
		{Lat: 10, Lng: 179},
		{Lat: 20, Lng: 179},
		{Lat: 20, Lng: -179},
		{Lat: 10, Lng: -179},
	}
	if err := ValidateRing(cross); err == nil {
		t.Fatal("antimeridian-crossing polygon must be rejected")
	}
	// A normal polygon sitting near but not spanning the dateline is fine.
	near := []LatLng{
		{Lat: 10, Lng: 174}, {Lat: 20, Lng: 174}, {Lat: 20, Lng: 179},
		{Lat: 10, Lng: 179},
	}
	if err := ValidateRing(near); err != nil {
		t.Fatalf("polygon within [174,179] should be accepted: %v", err)
	}
}

func TestPointInRing_Interior(t *testing.T) {
	r := square(0, 0, 1)
	if PointInRing(LatLng{Lat: 0, Lng: 0}, r) != Inside {
		t.Fatal("center should be inside")
	}
	if PointInRing(LatLng{Lat: 5, Lng: 5}, r) != Outside {
		t.Fatal("far point should be outside")
	}
}

func TestPointInRing_Boundary(t *testing.T) {
	r := square(0, 0, 1)
	cases := []LatLng{
		{Lat: -1, Lng: -1}, // vertex
		{Lat: 0, Lng: -1},  // midpoint of edge
		{Lat: 1, Lng: 1},   // opposite vertex
		{Lat: -1, Lng: 0},  // closing edge midpoint
	}
	for _, p := range cases {
		if PointInRing(p, r) != Boundary {
			t.Fatalf("point %v on boundary must classify as Boundary (boundary is inside)", p)
		}
	}
}

func TestPointInRing_VertexTie(t *testing.T) {
	// Ray through two vertices must still give a stable classification.
	r := []LatLng{
		{Lat: 0, Lng: 0},
		{Lat: 2, Lng: 0},
		{Lat: 2, Lng: 2},
		{Lat: 0, Lng: 2},
	}
	p := LatLng{Lat: 0, Lng: 1} // on the bottom edge, collinear with vertices
	if PointInRing(p, r) != Boundary {
		t.Fatal("point on edge must be Boundary")
	}
}

func TestHaversine(t *testing.T) {
	// One degree of longitude at the equator ~= 111.195 km.
	d := HaversineMeters(LatLng{Lat: 0, Lng: 0}, LatLng{Lat: 0, Lng: 1})
	if math.Abs(d-111195) > 200 {
		t.Fatalf("equatorial degree distance = %.1f, want ~111195", d)
	}
	if d := HaversineMeters(LatLng{Lat: 45, Lng: 10}, LatLng{Lat: 45, Lng: 10}); d != 0 {
		t.Fatalf("identical points distance = %v, want 0", d)
	}
}

func TestBounds(t *testing.T) {
	minLat, minLng, maxLat, maxLng := Bounds(square(2, 3, 1))
	if minLat != 2 || minLng != 1 || maxLat != 4 || maxLng != 3 {
		t.Fatalf("bounds = (%v,%v)-(%v,%v)", minLat, minLng, maxLat, maxLng)
	}
}
