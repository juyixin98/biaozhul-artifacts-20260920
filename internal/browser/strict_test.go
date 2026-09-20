package browser_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"sitevitals/internal/browser"
	"sitevitals/internal/demo"
	"sitevitals/internal/models"
	"sitevitals/internal/whitelist"
)

// TestCollect_StrictSubresources_BlocksOffList uses two local servers: the
// page origin is whitelisted, the image origin is not. In strict mode the
// subresource must be blocked in the browser and marked on the waterfall,
// while the page navigation itself still succeeds.
func TestCollect_StrictSubresources_BlocksOffList(t *testing.T) {
	skipNoChrome(t)

	pageSrv := httptest.NewServer(demo.NewServer())
	t.Cleanup(pageSrv.Close)
	// A second origin serving a valid image.
	externalSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/svg+xml")
		_, _ = w.Write([]byte(`<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>`))
	}))
	t.Cleanup(externalSrv.Close)

	inst := startInstance(t)
	matcher := whitelist.NewMatcher([]models.Site{
		{Name: "page", Origin: pageSrv.URL, PathPrefix: "/", Enabled: true},
	})
	prof, _ := whitelist.Profile(models.ViewportDesktop)
	c := browser.NewCollector(inst, true) // strict
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	m, _, err := c.Collect(ctx, browser.CollectOptions{
		TargetURL: pageSrv.URL + "/external?img=" + externalSrv.URL + "/pic.svg",
		Profile:   prof, Matcher: matcher,
		NavTimeout: 20 * time.Second, Settle: 2 * time.Second,
	})
	if err != nil {
		t.Fatalf("document must still load in strict mode: %v", err)
	}
	var sawBlocked bool
	for _, v := range m.Violations {
		if v.Stage == "subresource" && v.Kind == "not_whitelisted" && v.Blocked {
			sawBlocked = true
		}
	}
	if !sawBlocked {
		t.Fatalf("expected a blocked subresource violation, got %+v", m.Violations)
	}
	var blockedRes bool
	for _, r := range m.Resources {
		if r.URL == externalSrv.URL+"/pic.svg" && r.Blocked {
			blockedRes = true
		}
	}
	if !blockedRes {
		t.Fatalf("waterfall must mark the external image blocked: %+v", m.Resources)
	}
}
