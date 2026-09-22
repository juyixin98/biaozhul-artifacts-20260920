// Package geometry implements the local, dependency-free geospatial primitives
// used by GeoTerritory: polygon validation, point-in-polygon tests and the
// Haversine distance. There is no external geometry service involved.
package geometry

import (
	"errors"
	"fmt"
	"math"
)

// Eps is the tolerance used for floating point comparisons on coordinates
// expressed in degrees. ~1e-9 degrees is sub-millimetric, far below the
// precision of any real input while absorbing floating point rounding.
const Eps = 1e-9

// LatLng is a WGS-84 latitude/longitude pair in degrees.
type LatLng struct {
	Lat float64 `json:"lat"`
	Lng float64 `json:"lng"`
}

// ValidateCoordinate checks the legal WGS-84 coordinate box.
func ValidateCoordinate(p LatLng) error {
	if math.IsNaN(p.Lat) || math.IsNaN(p.Lng) || math.IsInf(p.Lat, 0) || math.IsInf(p.Lng, 0) {
		return errors.New("coordinate must be a finite number")
	}
	if p.Lat < -90 || p.Lat > 90 {
		return fmt.Errorf("latitude %.8f out of range [-90, 90]", p.Lat)
	}
	if p.Lng < -180 || p.Lng > 180 {
		return fmt.Errorf("longitude %.8f out of range [-180, 180]", p.Lng)
	}
	return nil
}

// ValidateRing validates a closed polygon ring. The ring is given WITHOUT a
// repeated closing vertex: storage stores each vertex once and every edge
// (including the implicit edge from the last vertex back to the first) is
// checked.
//
// Rules:
//   - at least 3 vertices, all distinct (near-duplicates within Eps rejected);
//   - every coordinate inside the legal WGS-84 box;
//   - any edge whose longitude delta is >= 180 degrees is REJECTED: polygons
//     crossing (or exactly spanning) the antimeridian at +/-180 are not
//     supported and must not be silently interpreted the wrong way;
//   - no self-intersection, including a vertex lying on a non-incident edge;
//   - non-zero signed area (collinear / fully degenerate rings rejected).
func ValidateRing(ring []LatLng) error {
	n := len(ring)
	if n < 3 {
		return fmt.Errorf("polygon needs at least 3 distinct vertices, got %d", n)
	}
	for i, v := range ring {
		if err := ValidateCoordinate(v); err != nil {
			return fmt.Errorf("vertex %d: %w", i, err)
		}
	}
	// All vertices must be distinct (covers repeated closing vertices too,
	// since the ring is stored without an implicit repeat).
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			if pointsAlmostEqual(ring[i], ring[j]) {
				return fmt.Errorf("vertex %d duplicates vertex %d; vertices must be distinct", j, i)
			}
		}
	}
	for i := 0; i < n; i++ {
		a, b := ring[i], ring[(i+1)%n]
		// Explicit, never silent antimeridian handling.
		if math.Abs(b.Lng-a.Lng) >= 180-Eps {
			return fmt.Errorf("edge from vertex %d to vertex %d spans >= 180 degrees of longitude: polygons crossing the antimeridian (+/-180) are not supported", i, (i+1)%n)
		}
	}
	// Non-adjacent edges must not intersect (proper crossing or touching).
	for i := 0; i < n; i++ {
		e1a, e1b := ring[i], ring[(i+1)%n]
		for j := i + 1; j < n; j++ {
			if j == i+1 || (i == 0 && j == n-1) {
				continue // adjacent edges share exactly one endpoint
			}
			e2a, e2b := ring[j], ring[(j+1)%n]
			if segmentsIntersect(e1a, e1b, e2a, e2b) {
				return fmt.Errorf("edge %d-%d intersects edge %d-%d: self-intersecting polygons are not allowed", i, (i+1)%n, j, (j+1)%n)
			}
		}
	}
	// Every vertex must lie off every non-incident edge (T-junction / spike).
	for i := 0; i < n; i++ {
		v := ring[i]
		for j := 0; j < n; j++ {
			if j == i || j == (i-1+n)%n || j == (i+1)%n {
				continue
			}
			a, b := ring[j], ring[(j+1)%n]
			if PointOnSegment(v, a, b) {
				return fmt.Errorf("vertex %d lies on edge %d-%d: degenerate polygons are not allowed", i, j, (j+1)%n)
			}
		}
	}
	if math.Abs(SignedArea(ring)) <= Eps {
		return errors.New("polygon area is zero: degenerate polygons are not allowed")
	}
	return nil
}

func pointsAlmostEqual(a, b LatLng) bool {
	dx, dy := a.Lng-b.Lng, a.Lat-b.Lat
	return dx*dx+dy*dy <= Eps*Eps
}

// SignedArea returns the shoelace signed area in square degrees. Positive for
// counter-clockwise rings.
func SignedArea(ring []LatLng) float64 {
	var area float64
	n := len(ring)
	for i := 0; i < n; i++ {
		a, b := ring[i], ring[(i+1)%n]
		area += a.Lng*b.Lat - b.Lng*a.Lat
	}
	return area / 2
}

// Bounds returns the bounding box (minLat, minLng, maxLat, maxLng).
func Bounds(ring []LatLng) (minLat, minLng, maxLat, maxLng float64) {
	minLat, maxLat = ring[0].Lat, ring[0].Lat
	minLng, maxLng = ring[0].Lng, ring[0].Lng
	for _, v := range ring[1:] {
		minLat = math.Min(minLat, v.Lat)
		maxLat = math.Max(maxLat, v.Lat)
		minLng = math.Min(minLng, v.Lng)
		maxLng = math.Max(maxLng, v.Lng)
	}
	return
}

// Location is the result of a point-in-polygon test.
type Location int

const (
	Outside Location = iota
	Boundary
	Inside
)

// PointInRing classifies p against a validated ring.
//
// Boundary rule (documented and enforced consistently): a point lying exactly
// on an edge, including a polygon vertex or a point on the implicit closing
// edge, is treated as INSIDE the region.
func PointInRing(p LatLng, ring []LatLng) Location {
	n := len(ring)
	for i := 0; i < n; i++ {
		a, b := ring[i], ring[(i+1)%n]
		if PointOnSegment(p, a, b) {
			return Boundary
		}
	}
	// Standard ray casting to +x. Endpoint ties are broken by the strict
	// inequality pair so a ray passing through a vertex is counted once.
	inside := false
	for i := 0; i < n; i++ {
		a, b := ring[i], ring[(i+1)%n]
		if (a.Lat > p.Lat) != (b.Lat > p.Lat) {
			xCross := a.Lng + (p.Lat-a.Lat)*(b.Lng-a.Lng)/(b.Lat-a.Lat)
			if p.Lng < xCross {
				inside = !inside
			}
		}
	}
	if inside {
		return Inside
	}
	return Outside
}

// PointOnSegment reports whether p lies on segment ab, using a scale-relative
// collinearity tolerance plus a bounding-box check.
func PointOnSegment(p, a, b LatLng) bool {
	dx, dy := b.Lng-a.Lng, b.Lat-a.Lat
	cross := dx*(p.Lat-a.Lat) - dy*(p.Lng-a.Lng)
	chord2 := dx*dx + dy*dy
	tol := Eps * math.Max(1, chord2)
	if math.Abs(cross) > tol {
		return false
	}
	return p.Lng >= math.Min(a.Lng, b.Lng)-Eps &&
		p.Lng <= math.Max(a.Lng, b.Lng)+Eps &&
		p.Lat >= math.Min(a.Lat, b.Lat)-Eps &&
		p.Lat <= math.Max(a.Lat, b.Lat)+Eps
}

// segmentsIntersect reports whether the closed segments ab and cd share any
// point (proper intersection or endpoint touch), used for self-intersection
// checks on non-adjacent edges.
func segmentsIntersect(a, b, c, d LatLng) bool {
	o1 := cross(a, b, c)
	o2 := cross(a, b, d)
	o3 := cross(c, d, a)
	o4 := cross(c, d, b)
	if math.Abs(o1) <= Eps*math.Max(1, dist2(b, a)) && PointOnSegment(c, a, b) {
		return true
	}
	if math.Abs(o2) <= Eps*math.Max(1, dist2(b, a)) && PointOnSegment(d, a, b) {
		return true
	}
	if math.Abs(o3) <= Eps*math.Max(1, dist2(d, c)) && PointOnSegment(a, c, d) {
		return true
	}
	if math.Abs(o4) <= Eps*math.Max(1, dist2(d, c)) && PointOnSegment(b, c, d) {
		return true
	}
	return (o1 > 0) != (o2 > 0) && o1 != 0 && o2 != 0 &&
		(o3 > 0) != (o4 > 0) && o3 != 0 && o4 != 0
}

func cross(o, a, b LatLng) float64 {
	return (a.Lng-o.Lng)*(b.Lat-o.Lat) - (a.Lat-o.Lat)*(b.Lng-o.Lng)
}

func dist2(a, b LatLng) float64 {
	dx, dy := a.Lng-b.Lng, a.Lat-b.Lat
	return dx*dx + dy*dy
}

// earthRadiusM is the mean Earth radius used for Haversine distances.
const earthRadiusM = 6_371_000.0

// HaversineMeters returns the great-circle distance between two points in
// meters. Coordinates are assumed validated.
func HaversineMeters(a, b LatLng) float64 {
	lat1 := a.Lat * math.Pi / 180
	lat2 := b.Lat * math.Pi / 180
	dLat := (b.Lat - a.Lat) * math.Pi / 180
	dLng := (b.Lng - a.Lng) * math.Pi / 180
	h := math.Sin(dLat/2)*math.Sin(dLat/2) +
		math.Cos(lat1)*math.Cos(lat2)*math.Sin(dLng/2)*math.Sin(dLng/2)
	return 2 * earthRadiusM * math.Asin(math.Sqrt(h))
}
