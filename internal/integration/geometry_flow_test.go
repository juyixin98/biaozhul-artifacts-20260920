package integration

import (
	"fmt"
	"net/http"
	"sync/atomic"
	"testing"
	"time"
)

// Invalid polygons must be rejected at the API with 400 and never stored.
func TestPolygonValidation(t *testing.T) {
	cases := []struct {
		name string
		body map[string]any
	}{
		{
			"antimeridian",
			map[string]any{"name": "bad-anti-" + uniq(), "priority": 2, "vertices": []map[string]any{
				{"lat": 10, "lng": 179}, {"lat": 20, "lng": 179},
				{"lat": 20, "lng": -179}, {"lat": 10, "lng": -179},
			}},
		},
		{
			"self-intersecting bow-tie",
			map[string]any{"name": "bad-bow-" + uniq(), "priority": 2, "vertices": []map[string]any{
				{"lat": 0, "lng": 0}, {"lat": 2, "lng": 2},
				{"lat": 0, "lng": 2}, {"lat": 2, "lng": 0},
			}},
		},
		{
			"too few vertices",
			map[string]any{"name": "bad-few-" + uniq(), "priority": 2, "vertices": []map[string]any{
				{"lat": 0, "lng": 0}, {"lat": 1, "lng": 1},
			}},
		},
		{
			"latitude out of range",
			map[string]any{"name": "bad-lat-" + uniq(), "priority": 2, "vertices": []map[string]any{
				{"lat": 0, "lng": 0}, {"lat": 91, "lng": 0}, {"lat": 0, "lng": 1},
			}},
		},
		{
			"repeated vertex",
			map[string]any{"name": "bad-rep-" + uniq(), "priority": 2, "vertices": []map[string]any{
				{"lat": 0, "lng": 0}, {"lat": 1, "lng": 0},
				{"lat": 1, "lng": 1}, {"lat": 0, "lng": 0},
			}},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, body := doJSON(t, http.MethodPost, "/v1/regions", env.keyA, tc.body)
			if status != http.StatusBadRequest {
				t.Fatalf("want 400, got %d: %v", status, body)
			}
			if body["detail"] == nil && body["error"] == nil {
				t.Fatal("expected an error detail")
			}
		})
	}

	// Antimeridian-crossing bbox query must be rejected too.
	status, body := doJSON(t, http.MethodGet,
		"/v1/points/bbox?min_lat=0&max_lat=10&min_lng=170&max_lng=-170", env.keyA, nil)
	if status != http.StatusBadRequest {
		t.Fatalf("antimeridian bbox want 400, got %d: %v", status, body)
	}
}

// Boundary points are inside; overlap resolution follows priority then id.
func TestBoundaryAndOverlap(t *testing.T) {
	suffix := uniq()
	// Two overlapping 4x4 squares. p1 has priority 1; p2 priority 2.
	hi := squareRegion("zone-hi-"+suffix, 1, 0, 0, 2)
	lo := squareRegion("zone-lo-"+suffix, 2, 0.5, 0.5, 2)

	for _, reg := range []map[string]any{hi, lo} {
		status, body := doJSON(t, http.MethodPost, "/v1/regions", env.keyA, reg)
		if status != http.StatusAccepted {
			t.Fatalf("publish %s: status=%d body=%v", reg["name"], status, body)
		}
		waitForJob(t, env.keyA, body["job_id"].(float64))
	}

	// Point on the edge of the high-priority square: boundary => assigned to
	// that zone and flagged on_boundary. The edge at lng=-2 runs lat -2..2.
	batch := map[string]any{"points": []map[string]any{
		{"external_id": "edge-" + suffix, "lat": 1.234, "lng": -2.0},
		// A vertex itself.
		{"external_id": "vertex-" + suffix, "lat": 2.0, "lng": 2.0},
		// Overlap interior: priority 1 region must win.
		{"external_id": "overlap-" + suffix, "lat": 0.5, "lng": 0.5},
		// Only inside the priority-2 square (east of hi's right edge at +2).
		{"external_id": "lo-only-" + suffix, "lat": 2.4, "lng": 2.4},
		// Outside both.
		{"external_id": "outside-" + suffix, "lat": 50, "lng": 50},
	}}
	status, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, batch)
	if status != http.StatusOK {
		t.Fatalf("batch status=%d body=%v", status, body)
	}
	if body["failed"].(float64) != 0 {
		t.Fatalf("batch failures: %v", body["results"])
	}

	get := func(ext string) map[string]any {
		st, b := doJSON(t, http.MethodGet, "/v1/points/"+ext, env.keyA, nil)
		if st != http.StatusOK {
			t.Fatalf("get %s: %d %v", ext, st, b)
		}
		return b["point"].(map[string]any)
	}

	edge := get("edge-" + suffix)
	if edge["region_id"] == nil || edge["region_id"].(float64) == 0 {
		t.Fatalf("edge point must be assigned, got %v", edge)
	}
	if edge["on_boundary"] != true {
		t.Fatalf("edge point must be on_boundary, got %v", edge)
	}
	vertex := get("vertex-" + suffix)
	if vertex["region_id"].(float64) == 0 || vertex["on_boundary"] != true {
		t.Fatalf("vertex point must be boundary-assigned, got %v", vertex)
	}
	overlap := get("overlap-" + suffix)

	// Resolve region ids via the region list.
	st, cat := doJSON(t, http.MethodGet, "/v1/regions", env.keyA, nil)
	if st != http.StatusOK {
		t.Fatalf("list regions: %v", cat)
	}
	ids := map[string]float64{}
	for _, r := range cat["regions"].([]any) {
		rm := r.(map[string]any)
		ids[rm["name"].(string)] = rm["id"].(float64)
	}
	if overlap["region_id"].(float64) != ids["zone-hi-"+suffix] {
		t.Fatalf("overlap point must go to priority-1 zone, got %v", overlap)
	}
	loOnly := get("lo-only-" + suffix)
	if loOnly["region_id"].(float64) != ids["zone-lo-"+suffix] {
		t.Fatalf("lo-only point must go to priority-2 zone, got %v", loOnly)
	}
	outside := get("outside-" + suffix)
	if outside["region_id"].(float64) != 0 {
		t.Fatalf("outside point must be unassigned (region_id 0), got %v", outside)
	}
}

// Equal-priority regions break ties by smaller region id.
func TestOverlapTieBreakByID(t *testing.T) {
	suffix := uniq()
	// Same square geometry, same priority; different names => different ids.
	a := squareRegion("tie-a-"+suffix, 3, 60, 30, 2)
	b := squareRegion("tie-b-"+suffix, 3, 60, 30, 2)
	var idA, idB float64
	for _, reg := range []map[string]any{a, b} {
		status, body := doJSON(t, http.MethodPost, "/v1/regions", env.keyA, reg)
		if status != http.StatusAccepted {
			t.Fatalf("publish: %d %v", status, body)
		}
		waitForJob(t, env.keyA, body["job_id"].(float64))
		st, listed := doJSON(t, http.MethodGet, "/v1/regions", env.keyA, nil)
		if st != http.StatusOK {
			t.Fatalf("list: %v", listed)
		}
		for _, r := range listed["regions"].([]any) {
			rm := r.(map[string]any)
			switch rm["name"] {
			case "tie-a-" + suffix:
				idA = rm["id"].(float64)
			case "tie-b-" + suffix:
				idB = rm["id"].(float64)
			}
		}
	}
	batch := map[string]any{"points": []map[string]any{
		{"external_id": "tie-pt-" + suffix, "lat": 60, "lng": 30},
	}}
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, batch); st != http.StatusOK {
		t.Fatalf("batch: %d %v", st, body)
	}
	_, pb := doJSON(t, http.MethodGet, "/v1/points/tie-pt-"+suffix, env.keyA, nil)
	got := pb["point"].(map[string]any)["region_id"].(float64)
	want := idA
	if idB < idA {
		want = idB
	}
	if got != want {
		t.Fatalf("equal-priority overlap must pick smaller region id: got %v want %v (a=%v b=%v)", got, want, idA, idB)
	}
}

func uniq() string {
	return fmt.Sprintf("%d%d", time.Now().UnixNano(), uniqCounter.Add(1))
}

var uniqCounter atomic.Int64
