package integration

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"geoterritory/internal/api"
	"geoterritory/internal/models"
	"geoterritory/internal/service"

	"github.com/gin-gonic/gin"
)

func httpServer() *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	g := r.Group("/api/v1")
	g.Use(api.AuthMiddleware(testEnv.db))
	h := api.NewHandler(service.NewRegionService(testEnv.db), service.NewPointService(testEnv.db))
	h.Register(g)
	return r
}

func do(t *testing.T, r *gin.Engine, method, path, key, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("X-API-Key", key)
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func TestHTTP_AuthRequired(t *testing.T) {
	r := httpServer()
	for _, tc := range []struct{ method, path string }{
		{"GET", "/api/v1/regions"},
		{"GET", "/api/v1/points/nearest?lat=0&lng=0&n=1"},
		{"POST", "/api/v1/points/batch"},
	} {
		w := do(t, r, tc.method, tc.path, "", "")
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s without key: code %d, want 401", tc.method, tc.path, w.Code)
		}
	}
	if w := do(t, r, "GET", "/api/v1/regions", "key-does-not-exist", ""); w.Code != http.StatusUnauthorized {
		t.Fatalf("bogus key: code %d, want 401", w.Code)
	}
}

// TestHTTP_TenantIsolation seeds a point in each org and proves one org's key
// can never read or mutate the other's data through the API.
func TestHTTP_TenantIsolation(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "acme")
	e.resetOrg(t, "beta")
	acme, beta := e.orgs["acme"], e.orgs["beta"]

	var ka, kb models.Organization
	e.db.First(&ka, acme)
	e.db.First(&kb, beta)

	if _, err := e.points.BatchImport(acme, []service.BatchItem{{ExternalID: "SECRET", Lat: 1, Lng: 1}}); err != nil {
		t.Fatal(err)
	}
	r := httpServer()

	// Beta key fetching Acme's external id -> 404, never 200.
	w := do(t, r, "GET", "/api/v1/points/SECRET", kb.APIKey, "")
	if w.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant read returned %d: %s", w.Code, w.Body.String())
	}
	// Bbox for beta around the secret point must be empty.
	w = do(t, r, "GET", "/api/v1/points/bbox/search?min_lat=0&max_lat=2&min_lng=0&max_lng=2", kb.APIKey, "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"count":0`) {
		t.Fatalf("cross-tenant bbox leaked data: %d %s", w.Code, w.Body.String())
	}
	// Beta attempting an update on Acme's external id creates no cross-tenant
	// effect: 404 on lookup.
	w = do(t, r, "PATCH", "/api/v1/points/SECRET", kb.APIKey, `{"lat":9,"lng":9,"expected_version":1}`)
	if w.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant patch returned %d", w.Code)
	}
	// Acme's own key still sees the point untouched.
	w = do(t, r, "GET", "/api/v1/points/SECRET", ka.APIKey, "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"lat":1`) {
		t.Fatalf("acme read broken after cross-tenant attempts: %d %s", w.Code, w.Body.String())
	}
}

// TestHTTP_RegionLifecycle drives create -> poll job -> get region versions
// through HTTP and validates the antimeridian 400 path.
func TestHTTP_RegionLifecycle(t *testing.T) {
	e := testEnv
	e.resetOrg(t, "gamma")
	gamma := e.orgs["gamma"]
	var kg models.Organization
	e.db.First(&kg, gamma)
	r := httpServer()

	body := `{"name":"httpbox","priority":2,"polygon":[{"lng":-1,"lat":-1},{"lng":1,"lat":-1},{"lng":1,"lat":1},{"lng":-1,"lat":1}]}`
	w := do(t, r, "POST", "/api/v1/regions", kg.APIKey, body)
	if w.Code != http.StatusAccepted {
		t.Fatalf("create region: %d %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"status":"pending"`) {
		t.Fatalf("response should include pending job: %s", w.Body.String())
	}
	e.drainJobs(t)
	w = do(t, r, "GET", "/api/v1/regions", kg.APIKey, "")
	if !strings.Contains(w.Body.String(), "httpbox") {
		t.Fatalf("region list missing: %s", w.Body.String())
	}

	// Antimeridian polygon rejected at the HTTP boundary.
	bad := `{"name":"pacific","priority":1,"polygon":[{"lng":179,"lat":10},{"lng":-179,"lat":10},{"lng":-179,"lat":20},{"lng":179,"lat":20}]}`
	w = do(t, r, "POST", "/api/v1/regions", kg.APIKey, bad)
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "antimeridian") {
		t.Fatalf("antimeridian should 400 with clear message, got %d %s", w.Code, w.Body.String())
	}
}
