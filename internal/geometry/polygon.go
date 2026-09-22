// Package geometry implements self-contained planar/spherical helpers used by
// GeoTerritory: polygon validation and point-in-polygon tests, plus the
// Haversine distance used by nearest-neighbour queries.
//
// Supported coordinate space:
//   - latitude  in [-90, 90]
//   - longitude in [-180, 180]
//
// Polygons that cross the antimeridian (the ±180° meridian) are REJECTED,
// never silently split or reinterpreted. A territory that spans the
// antimeridian must be modeled as two polygons (split along the 180° line);
// GeoTerritory treats each polygon as an independent territory with its own
// priority, or the deployment can be migrated to a projected coordinate
// system. This keeps point-in-polygon semantics simple and auditable.
package geometry

import (
	"errors"
	"fmt"
	"math"
)

// Vertex is one (longitude, latitude) pair. Longitude is X and latitude Y,
// matching the GeoJSON [lng, lat] convention.
type Vertex struct {
	Lng float64 `json:"lng"`
	Lat float64 `json:"lat"`
}

// eps is the tolerance for all exact-on-boundary and degeneracy checks.
// It is roughly 0.1 millimetre on Earth and absorbs JSON round-tripping.
const eps = 1e-9

var (
	ErrTooFewVertices   = errors.New("polygon must have at least 3 distinct vertices")
	ErrDuplicateVertex  = errors.New("consecutive vertices must be distinct (zero-length edge)")
	ErrCoordinateRange  = errors.New("coordinate out of range: latitude must be in [-90,90], longitude in [-180,180]")
	ErrDegenerate       = errors.New("polygon is degenerate: signed area is zero")
	ErrSelfIntersecting = errors.New("polygon edges intersect: polygons must be simple (no self-intersection)")
	ErrAntimeridian     = errors.New("polygon crosses the antimeridian (an edge spans more than 180 degrees of longitude); split it along the 180th meridian and submit two polygons")
)

// ValidatePolygon validates a closed polygon supplied as open-ring vertices:
// callers submit v[0..n-1] and the closure edge v[n-1] -> v[0] is implicit.
//
// It enforces, in order:
//  1. finite coordinates and valid lat/lng ranges;
//  2. at least 3 distinct vertices;
//  3. no consecutive duplicate vertices (zero-length edges), closure included;
//  4. no antimeridian crossing (any edge with |Δlng| > 180);
//  5. non-zero signed area (no collinear/degenerate ring);
//  6. no intersections between non-adjacent edges (simple polygon only).
func ValidatePolygon(verts []Vertex) error {
	if len(verts) < 3 {
		return ErrTooFewVertices
	}

	distinct := make(map[Vertex]bool, len(verts))
	for _, v := range verts {
		if !isFinite(v.Lat) || !isFinite(v.Lng) {
			return ErrCoordinateRange
		}
		if v.Lat < -90-eps || v.Lat > 90+eps || v.Lng < -180-eps || v.Lng > 180+eps {
			return ErrCoordinateRange
		}
		distinct[quantize(v)] = true
	}
	if len(distinct) < 3 {
		return ErrTooFewVertices
	}

	n := len(verts)
	for i := 0; i < n; i++ {
		a, b := verts[i], verts[(i+1)%n]
		if a == b { // after closure; covers trailing duplicate of v[0] too
			return ErrDuplicateVertex
		}
		if math.Abs(b.Lng-a.Lng) > 180 {
			return fmt.Errorf("%w: edge (%g,%g)->(%g,%g)", ErrAntimeridian, a.Lng, a.Lat, b.Lng, b.Lat)
		}
	}

	// Self-intersection is checked before the zero-area check: a bow-tie
	// ring has a signed area of zero (its lobes cancel) but the defect the
	// caller cares about is that it crosses itself.
	for i := 0; i < n; i++ {
		a1 := verts[i]
		a2 := verts[(i+1)%n]
		for j := i + 1; j < n; j++ {
			// Skip adjacent edges, including the (n-1,0) pair wrapping around.
			if j == i+1 || (i == 0 && j == n-1) {
				continue
			}
			b1 := verts[j]
			b2 := verts[(j+1)%n]
			if segmentsIntersect(a1, a2, b1, b2) {
				return fmt.Errorf("%w: edge %d-%d crosses edge %d-%d", ErrSelfIntersecting, i, (i+1)%n, j, (j+1)%n)
			}
		}
	}

	if math.Abs(signedArea(verts)) < eps {
		return ErrDegenerate
	}
	return nil
}

// PointInPolygon reports whether p lies inside or on the boundary of the
// simple polygon verts.
//
// Boundary rule (inclusive): a point whose coordinates fall exactly on an
// edge, including a vertex, is INSIDE the polygon. The boundary check runs
// before the ray-cast so floating-point boundary cases are never reported
// as outside. Inputs are assumed validated by ValidatePolygon.
func PointInPolygon(p Vertex, verts []Vertex) bool {
	n := len(verts)
	for i := 0; i < n; i++ {
		if pointOnSegment(p, verts[i], verts[(i+1)%n]) {
			return true
		}
	}

	// Standard ray-casting to +x infinity, with the half-open interval rule
	// (aLat <= p.lat < b.lat or bLat <= p.lat < aLat) so vertices are
	// counted once.
	inside := false
	for i, j := 0, n-1; i < n; j, i = i, i+1 {
		vi, vj := verts[i], verts[j]
		if (vi.Lat > p.Lat) != (vj.Lat > p.Lat) {
			xAtRay := (vj.Lng-vi.Lng)*(p.Lat-vi.Lat)/(vj.Lat-vi.Lat) + vi.Lng
			if p.Lng < xAtRay {
				inside = !inside
			}
		}
	}
	return inside
}

// HaversineMeters returns great-circle distance between two points in metres.
func HaversineMeters(a, b Vertex) float64 {
	const earthRadiusMeters = 6371008.8 // mean Earth radius (IUGG R1)
	lat1 := a.Lat * math.Pi / 180
	lat2 := b.Lat * math.Pi / 180
	dLat := (b.Lat - a.Lat) * math.Pi / 180
	dLng := (b.Lng - a.Lng) * math.Pi / 180
	h := math.Sin(dLat/2)*math.Sin(dLat/2) +
		math.Cos(lat1)*math.Cos(lat2)*math.Sin(dLng/2)*math.Sin(dLng/2)
	return 2 * earthRadiusMeters * math.Asin(math.Sqrt(h))
}

// BBox is a simple lat/lng axis-aligned bounding box. It does not support
// crossing the antimeridian: callers must enforce MinLng <= MaxLng.
type BBox struct {
	MinLat, MaxLat, MinLng, MaxLng float64
}

// Contains reports whether p is inside the box (borders inclusive).
func (b BBox) Contains(p Vertex) bool {
	return p.Lat >= b.MinLat && p.Lat <= b.MaxLat &&
		p.Lng >= b.MinLng && p.Lng <= b.MaxLng
}

// PolygonBBox returns the axis-aligned bounding box of a polygon.
func PolygonBBox(verts []Vertex) BBox {
	b := BBox{
		MinLat: math.Inf(1), MaxLat: math.Inf(-1),
		MinLng: math.Inf(1), MaxLng: math.Inf(-1),
	}
	for _, v := range verts {
		b.MinLat = math.Min(b.MinLat, v.Lat)
		b.MaxLat = math.Max(b.MaxLat, v.Lat)
		b.MinLng = math.Min(b.MinLng, v.Lng)
		b.MaxLng = math.Max(b.MaxLng, v.Lng)
	}
	return b
}

func signedArea(verts []Vertex) float64 {
	var area float64
	n := len(verts)
	for i := 0; i < n; i++ {
		a := verts[i]
		b := verts[(i+1)%n]
		area += a.Lng*b.Lat - b.Lng*a.Lat
	}
	return area / 2
}

// pointOnSegment tests whether p lies on segment ab using the cross product
// (collinearity) plus a bounding check, all within eps.
func pointOnSegment(p, a, b Vertex) bool {
	cross := (b.Lng-a.Lng)*(p.Lat-a.Lat) - (b.Lat-a.Lat)*(p.Lng-a.Lng)
	if math.Abs(cross) > eps {
		return false
	}
	return p.Lng >= math.Min(a.Lng, b.Lng)-eps &&
		p.Lng <= math.Max(a.Lng, b.Lng)+eps &&
		p.Lat >= math.Min(a.Lat, b.Lat)-eps &&
		p.Lat <= math.Max(a.Lat, b.Lat)+eps
}

// segmentsIntersect reports whether closed segments ab and cd share any
// point, using integer-ish orientation tests. Endpoint touching counts as an
// intersection (self-touching polygons are rejected).
func segmentsIntersect(a, b, c, d Vertex) bool {
	o1 := orient(a, b, c)
	o2 := orient(a, b, d)
	o3 := orient(c, d, a)
	o4 := orient(c, d, b)
	if o1 == 0 && onBox(c, a, b) {
		return true
	}
	if o2 == 0 && onBox(d, a, b) {
		return true
	}
	if o3 == 0 && onBox(a, c, d) {
		return true
	}
	if o4 == 0 && onBox(b, c, d) {
		return true
	}
	return o1 != o2 && o3 != o4
}

// orient returns the sign of the cross product (b-a)×(c-a), normalised to
// -1/0/+1 so the predicate is insensitive to float magnitude.
func orient(a, b, c Vertex) int {
	v := (b.Lng-a.Lng)*(c.Lat-a.Lat) - (b.Lat-a.Lat)*(c.Lng-a.Lng)
	switch {
	case v > eps:
		return 1
	case v < -eps:
		return -1
	default:
		return 0
	}
}

func onBox(p, a, b Vertex) bool {
	return p.Lng >= math.Min(a.Lng, b.Lng)-eps &&
		p.Lng <= math.Max(a.Lng, b.Lng)+eps &&
		p.Lat >= math.Min(a.Lat, b.Lat)-eps &&
		p.Lat <= math.Max(a.Lat, b.Lat)+eps
}

func isFinite(f float64) bool {
	return !math.IsNaN(f) && !math.IsInf(f, 0)
}

// quantize snaps a vertex to a fixed grid so map-key dedup shares the eps
// tolerance used elsewhere instead of raw float equality.
func quantize(v Vertex) Vertex {
	return Vertex{
		Lng: math.Round(v.Lng/eps) * eps,
		Lat: math.Round(v.Lat/eps) * eps,
	}
}
