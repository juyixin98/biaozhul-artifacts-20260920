package integration

import (
	"net/http"
	"testing"
)

// Bounding box query honors org scoping and filters.
func TestBBoxQuery(t *testing.T) {
	key := newOrg(t)
	suffix := uniq()
	publishAndWait(t, key, squareRegion("bbox-zone-"+suffix, 1, 0, 0, 2))

	pts := []map[string]any{
		{"external_id": "bb-in-" + suffix + "-1", "lat": 0.5, "lng": 0.5},
		{"external_id": "bb-in-" + suffix + "-2", "lat": -1.5, "lng": -1.5},
		{"external_id": "bb-out-" + suffix, "lat": 40, "lng": 40},
	}
	for _, p := range pts {
		if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", key,
			map[string]any{"points": []map[string]any{p}}); st != http.StatusOK || body["failed"].(float64) != 0 {
			t.Fatalf("seed %v: %v", p["external_id"], body)
		}
	}

	// Tight box around origin: two points, not the far one.
	st, body := doJSON(t, http.MethodGet,
		"/v1/points/bbox?min_lat=-2&max_lat=2&min_lng=-2&max_lng=2", key, nil)
	if st != http.StatusOK {
		t.Fatalf("bbox: %d %v", st, body)
	}
	if body["count"].(float64) != 2 {
		t.Fatalf("bbox count=%v want 2 (%v)", body["count"], body["points"])
	}

	// assigned_only still includes the in-zone points; a box around the far,
	// unassigned point with assigned_only=true must return zero.
	st, body = doJSON(t, http.MethodGet,
		"/v1/points/bbox?min_lat=39&max_lat=41&min_lng=39&max_lng=41&assigned_only=true",
		key, nil)
	if st != http.StatusOK {
		t.Fatalf("bbox assigned: %d", st)
	}
	if body["count"].(float64) != 0 {
		t.Fatalf("unassigned point must be excluded by assigned_only: %v", body["points"])
	}
}

// Nearest-N uses Haversine, limits to 50, breaks ties by id and is org scoped.
func TestNearestQuery(t *testing.T) {
	key := newOrg(t) // isolated org: ordering must not see other tests' points
	suffix := uniq()
	publishAndWait(t, key, squareRegion("near-zone-"+suffix, 1, 0, 0, 30))

	// Import points on the equator at whole-degree longitudes: distances are
	// monotonically increasing from the anchor at (0,0).
	batch := make([]map[string]any, 0, 5)
	for i := 1; i <= 5; i++ {
		batch = append(batch, map[string]any{
			"external_id": "near-" + suffix + "-" + itoa(i), "lat": 0, "lng": float64(i),
		})
	}
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", key,
		map[string]any{"points": batch}); st != http.StatusOK || body["failed"].(float64) != 0 {
		t.Fatalf("seed nearest: %v", body)
	}

	st, body := doJSON(t, http.MethodGet, "/v1/points/nearest?lat=0&lng=0&n=3", key, nil)
	if st != http.StatusOK {
		t.Fatalf("nearest: %d %v", st, body)
	}
	points := body["points"].([]any)
	if len(points) != 3 {
		t.Fatalf("want 3 nearest, got %d", len(points))
	}
	// Must be ordered ascending by distance.
	prev := 0.0
	for _, p := range points {
		pm := p.(map[string]any)
		d := pm["distance_m"].(float64)
		if d < prev {
			t.Fatalf("nearest not distance-sorted: %v", points)
		}
		prev = d
	}
	// Closest must be the lng=1 point around ~111.2 km.
	first := points[0].(map[string]any)
	if first["external_id"] != "near-"+suffix+"-1" {
		t.Fatalf("closest point wrong: %v", first)
	}
	if d := first["distance_m"].(float64); d < 110_000 || d > 112_000 {
		t.Fatalf("haversine 1deg at equator ~111.2km, got %.0f", d)
	}

	// n capped at 50.
	st, body = doJSON(t, http.MethodGet, "/v1/points/nearest?lat=0&lng=0&n=51", key, nil)
	if st != http.StatusBadRequest {
		t.Fatalf("n=51 must be 400, got %d", st)
	}
}

// Authorization isolation: org B can never read org A's points or regions,
// requests without a key are rejected, and an invalid key is rejected.
func TestAuthorizationIsolation(t *testing.T) {
	suffix := uniq()
	publishAndWait(t, env.keyA, squareRegion("secret-zone-"+suffix, 1, 0, 0, 2))
	ext := "secret-pt-" + suffix
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, map[string]any{
		"points": []map[string]any{{"external_id": ext, "lat": 0, "lng": 0}},
	}); st != http.StatusOK {
		t.Fatalf("seed secret: %v", body)
	}

	// No key.
	if st, _ := doJSON(t, http.MethodGet, "/v1/points/"+ext, "", nil); st != http.StatusUnauthorized {
		t.Fatalf("no key want 401, got %d", st)
	}
	// Bogus key.
	if st, _ := doJSON(t, http.MethodGet, "/v1/points/"+ext, "nope-"+uniq(), nil); st != http.StatusUnauthorized {
		t.Fatalf("bad key want 401, got %d", st)
	}
	// Org B cannot see A's point by the same external id.
	st, body := doJSON(t, http.MethodGet, "/v1/points/"+ext, env.keyB, nil)
	if st != http.StatusNotFound {
		t.Fatalf("org B must not see org A point, got %d %v", st, body)
	}
	// Bbox for B excludes A's point.
	st, body = doJSON(t, http.MethodGet,
		"/v1/points/bbox?min_lat=-90&max_lat=90&min_lng=-180&max_lng=180", env.keyB, nil)
	if st != http.StatusOK {
		t.Fatalf("bbox b: %d", st)
	}
	for _, p := range body["points"].([]any) {
		if p.(map[string]any)["external_id"] == ext {
			t.Fatal("org A point leaked into org B bbox results")
		}
	}
	// Nearest for B excludes A's point.
	st, body = doJSON(t, http.MethodGet, "/v1/points/nearest?lat=0&lng=0&n=50", env.keyB, nil)
	if st != http.StatusOK {
		t.Fatalf("nearest b: %d", st)
	}
	for _, p := range body["points"].([]any) {
		if p.(map[string]any)["external_id"] == ext {
			t.Fatal("org A point leaked into org B nearest results")
		}
	}
	// Region lists are isolated too.
	st, body = doJSON(t, http.MethodGet, "/v1/regions", env.keyB, nil)
	if st != http.StatusOK {
		t.Fatalf("regions b: %d", st)
	}
	for _, r := range body["regions"].([]any) {
		if r.(map[string]any)["name"] == "secret-zone-"+suffix {
			t.Fatal("org A region leaked into org B region list")
		}
	}
}
