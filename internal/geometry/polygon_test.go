package geometry

import (
	"errors"
	"math"
	"testing"
)

// Unit square ring (0,0)-(10,0)-(10,10)-(0,10).
func square() []Vertex {
	return []Vertex{{Lng: 0, Lat: 0}, {Lng: 10, Lat: 0}, {Lng: 10, Lat: 10}, {Lng: 0, Lat: 10}}
}

func TestValidatePolygon_OK(t *testing.T) {
	if err := ValidatePolygon(square()); err != nil {
		t.Fatalf("valid square rejected: %v", err)
	}
	// Triangle.
	tri := []Vertex{{Lng: 0, Lat: 0}, {Lng: 4, Lat: 0}, {Lng: 2, Lat: 3}}
	if err := ValidatePolygon(tri); err != nil {
		t.Fatalf("valid triangle rejected: %v", err)
	}
}

func TestValidatePolygon_TooFewVertices(t *testing.T) {
	cases := [][]Vertex{
		nil,
		{{Lng: 0, Lat: 0}},
		{{Lng: 0, Lat: 0}, {Lng: 1, Lat: 1}},
		// 3 entries but only 2 distinct points.
		{{Lng: 0, Lat: 0}, {Lng: 1, Lat: 1}, {Lng: 0, Lat: 0}},
	}
	for i, c := range cases {
		if err := ValidatePolygon(c); err != ErrTooFewVertices {
			t.Fatalf("case %d: want ErrTooFewVertices, got %v", i, err)
		}
	}
}

func TestValidatePolygon_CoordinateRange(t *testing.T) {
	bad := []Vertex{{Lng: 0, Lat: 0}, {Lng: 10, Lat: 0}, {Lng: 10, Lat: 91}}
	if err := ValidatePolygon(bad); err != ErrCoordinateRange {
		t.Fatalf("lat 91 should be rejected, got %v", err)
	}
	bad[2].Lat = 10
	bad[2].Lng = -181
	if err := ValidatePolygon(bad); err != ErrCoordinateRange {
		t.Fatalf("lng -181 should be rejected, got %v", err)
	}
	bad[2].Lng = 10
	bad[0].Lat = math.NaN()
	if err := ValidatePolygon(bad); err != ErrCoordinateRange {
		t.Fatalf("NaN should be rejected, got %v", err)
	}
}

func TestValidatePolygon_DuplicateConsecutive(t *testing.T) {
	ring := []Vertex{{Lng: 0, Lat: 0}, {Lng: 5, Lat: 0}, {Lng: 5, Lat: 0}, {Lng: 5, Lat: 10}, {Lng: 0, Lat: 10}}
	if err := ValidatePolygon(ring); err != ErrDuplicateVertex {
		t.Fatalf("zero-length edge, got %v", err)
	}
	// Closing edge of length zero (last point == first) is also rejected.
	closed := []Vertex{{Lng: 0, Lat: 0}, {Lng: 5, Lat: 0}, {Lng: 5, Lat: 10}, {Lng: 0, Lat: 10}, {Lng: 0, Lat: 0}}
	if err := ValidatePolygon(closed); err != ErrDuplicateVertex {
		t.Fatalf("duplicate closing vertex, got %v", err)
	}
}

func TestValidatePolygon_Degenerate(t *testing.T) {
	// Three collinear points: zero signed area.
	line := []Vertex{{Lng: 0, Lat: 0}, {Lng: 5, Lat: 0}, {Lng: 10, Lat: 0}}
	if err := ValidatePolygon(line); err != ErrDegenerate {
		t.Fatalf("collinear ring should be ErrDegenerate, got %v", err)
	}
}

func TestValidatePolygon_SelfIntersecting(t *testing.T) {
	// Bow-tie: (0,0)-(10,10)-(10,0)-(0,10).
	bowtie := []Vertex{{Lng: 0, Lat: 0}, {Lng: 10, Lat: 10}, {Lng: 10, Lat: 0}, {Lng: 0, Lat: 10}}
	if err := ValidatePolygon(bowtie); !errors.Is(err, ErrSelfIntersecting) {
		t.Fatalf("bowtie should be ErrSelfIntersecting, got %v", err)
	}
	// Self-touch: two unit squares joined at a single pinch point (1,1),
	// which is visited twice by non-adjacent edges.
	touch := []Vertex{
		{Lng: 0, Lat: 0}, {Lng: 1, Lat: 0}, {Lng: 1, Lat: 1}, {Lng: 2, Lat: 1},
		{Lng: 2, Lat: 2}, {Lng: 1, Lat: 2}, {Lng: 1, Lat: 1}, {Lng: 0, Lat: 1},
	}
	if err := ValidatePolygon(touch); !errors.Is(err, ErrSelfIntersecting) {
		t.Fatalf("self-touching ring should be ErrSelfIntersecting, got %v", err)
	}
}

func TestValidatePolygon_AntimeridianRejected(t *testing.T) {
	// Box around the Pacific: edge from +179 to -179 spans 358 degrees.
	ring := []Vertex{{Lng: 179, Lat: 10}, {Lng: -179, Lat: 10}, {Lng: -179, Lat: 20}, {Lng: 179, Lat: 20}}
	err := ValidatePolygon(ring)
	if err == nil {
		t.Fatalf("antimeridian-crossing polygon must be rejected, got nil")
	}
	if !errors.Is(err, ErrAntimeridian) {
		t.Fatalf("want ErrAntimeridian, got %v", err)
	}
	// A legitimate wide-but-not-crossing ring (358 deg the other way around
	// is indistinguishable in lng space; the explicit split contract means
	// such a shape must be submitted as two polygons) - ensure 179 -> -178 is
	// rejected while 179 -> 178 is fine.
	ok := []Vertex{{Lng: 179, Lat: 10}, {Lng: 178, Lat: 10}, {Lng: 178, Lat: 20}, {Lng: 179, Lat: 20}}
	if err := ValidatePolygon(ok); err != nil {
		t.Fatalf("non-crossing edge rejected: %v", err)
	}
}

func TestPointInPolygon_InteriorAndExterior(t *testing.T) {
	ring := square()
	inside := []Vertex{{Lng: 5, Lat: 5}, {Lng: 0.001, Lat: 0.001}, {Lng: 9.999, Lat: 9.999}}
	for _, p := range inside {
		if !PointInPolygon(p, ring) {
			t.Errorf("point %v should be inside", p)
		}
	}
	outside := []Vertex{{Lng: -1, Lat: 5}, {Lng: 5, Lat: 11}, {Lng: 11, Lat: 11}, {Lng: 5, Lat: -0.5}}
	for _, p := range outside {
		if PointInPolygon(p, ring) {
			t.Errorf("point %v should be outside", p)
		}
	}
}

// TestPointInPolygon_BoundaryInclusive pins the documented boundary rule:
// every point on an edge or on a vertex is INSIDE.
func TestPointInPolygon_BoundaryInclusive(t *testing.T) {
	ring := square()
	onBoundary := []Vertex{
		{Lng: 0, Lat: 0},     // vertex
		{Lng: 10, Lat: 10},   // vertex
		{Lng: 5, Lat: 0},     // bottom edge
		{Lng: 10, Lat: 5},    // right edge
		{Lng: 5, Lat: 10},    // top edge
		{Lng: 0, Lat: 5},     // left edge
		{Lng: 1e-10, Lat: 0}, // within eps of edge
		{Lng: 3, Lat: 6},     // interior control
	}
	for i, p := range onBoundary {
		if i == 7 {
			if !PointInPolygon(p, ring) {
				t.Errorf("interior control %v should be inside", p)
			}
			continue
		}
		if !PointInPolygon(p, ring) {
			t.Errorf("boundary point %v must be inside (inclusive boundary rule)", p)
		}
	}
}

func TestPointInPolygon_OnDiagonalEdge(t *testing.T) {
	// Triangle whose hypotenuse passes through (2,1.5) exactly.
	ring := []Vertex{{Lng: 0, Lat: 0}, {Lng: 4, Lat: 0}, {Lng: 4, Lat: 3}}
	if !PointInPolygon(Vertex{Lng: 2, Lat: 1.5}, ring) {
		t.Errorf("point on diagonal edge must be inside")
	}
	if PointInPolygon(Vertex{Lng: 2, Lat: 1.6}, ring) {
		t.Errorf("point just above diagonal must be outside")
	}
}

func TestHaversine_KnownDistances(t *testing.T) {
	// Paris -> London is ~343 km.
	d := HaversineMeters(Vertex{Lng: 2.3522, Lat: 48.8566}, Vertex{Lng: -0.1278, Lat: 51.5074})
	if d < 330e3 || d > 350e3 {
		t.Fatalf("Paris-London distance %g outside expected band", d)
	}
	if d0 := HaversineMeters(Vertex{Lng: 1, Lat: 1}, Vertex{Lng: 1, Lat: 1}); d0 != 0 {
		t.Fatalf("identical points distance = %g, want 0", d0)
	}
}

func TestBBoxContains(t *testing.T) {
	b := BBox{MinLat: 0, MaxLat: 10, MinLng: 0, MaxLng: 10}
	if !b.Contains(Vertex{Lng: 10, Lat: 10}) {
		t.Error("corner should be contained (inclusive)")
	}
	if b.Contains(Vertex{Lng: 10.0001, Lat: 5}) {
		t.Error("outside lng should not be contained")
	}
}
