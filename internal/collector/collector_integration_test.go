package collector

import (
	"context"
	"strings"
	"testing"
	"time"

	"sitevitals/internal/models"
	"sitevitals/internal/policy"
	"sitevitals/internal/testsite"
)

// startEnv launches the demo site on a random-ish fixed port and returns its
// origin and a cleanup function.
func startEnv(t *testing.T, addr, origin string) func() {
	t.Helper()
	if _, err := findChrome(); err != nil {
		t.Skipf("chromium unavailable: %v", err)
	}
	srv := testsite.New(addr)
	if err := srv.Start(); err != nil {
		t.Skipf("test site cannot bind %s: %v", addr, err)
	}
	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}
}

func checkerFor(t *testing.T, origins ...string) *policy.Checker {
	t.Helper()
	var sites []models.Site
	var rules []models.AllowedURL
	for i, o := range origins {
		id := uint64(i + 1)
		sites = append(sites, models.Site{ID: id, SchemeHost: o, Enabled: true})
		rules = append(rules, models.AllowedURL{ID: uint64(i + 1), SiteID: id, URLPattern: o + "/*", Enabled: true})
	}
	c, err := policy.Compile(sites, rules)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func metricByName(t *testing.T, res *Result, name string) *models.Metric {
	t.Helper()
	for _, m := range res.Metrics {
		if m.Name == name {
			return m
		}
	}
	t.Fatalf("metric %s missing", name)
	return nil
}

func TestCollectHappyPath(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18203", "http://127.0.0.1:18203")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18203")

	res, err := Collect(context.Background(), "http://127.0.0.1:18203/", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 20 * time.Second, SettleDelay: 1500 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, name := range []string{MetricNavTTFB, MetricNavLoad, MetricFCP, MetricLCP} {
		m := metricByName(t, res, name)
		if m.Status != models.MetricCollected {
			t.Errorf("%s status=%s detail=%s", name, m.Status, m.Detail)
		} else if m.ValueMS == nil || *m.ValueMS < 0 {
			t.Errorf("%s bad value %v", name, m.ValueMS)
		}
	}
	cls := metricByName(t, res, MetricCLS)
	if cls.Status != models.MetricCollected {
		t.Errorf("cls status=%s", cls.Status)
	}
	// Waterfall must contain the document + CSS/JS/PNG.
	kinds := map[string]bool{}
	for _, r := range res.Resources {
		kinds[r.ResourceType] = true
		if r.Status == "loaded" && (r.DurationMS == nil || *r.DurationMS < 0) {
			t.Errorf("resource %s missing duration", r.URL)
		}
	}
	for _, want := range []string{"Document", "Stylesheet", "Script", "Image"} {
		if !kinds[want] {
			t.Errorf("waterfall missing resource type %s (have %v)", want, kinds)
		}
	}
}

func TestCollectViewports(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18204", "http://127.0.0.1:18204")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18204")
	for _, vp := range []string{models.ViewportMobile, models.ViewportTablet, models.ViewportDesktop} {
		res, err := Collect(context.Background(), "http://127.0.0.1:18204/", Options{
			Viewport: vp, Checker: c, MaxRedirects: 5,
			NavTimeout: 20 * time.Second, SettleDelay: 500 * time.Millisecond,
		})
		if err != nil {
			t.Fatalf("viewport %s: %v", vp, err)
		}
		if res.FinalURL == "" {
			t.Errorf("viewport %s empty final url", vp)
		}
	}
}

func TestCollectExternalSubresourcesBlocked(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18205", "http://127.0.0.1:18205")
	defer cleanup()
	// Only the demo origin is allowed; example.com / 127.0.0.1:9 must be blocked.
	c := checkerFor(t, "http://127.0.0.1:18205")

	res, err := Collect(context.Background(), "http://127.0.0.1:18205/external", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 20 * time.Second, SettleDelay: time.Second,
	})
	if err != nil {
		t.Fatalf("page should load with blocked subresources: %v", err)
	}
	if res.BlockedResources < 3 {
		t.Errorf("blocked=%d want >=3 (external css/js/png)", res.BlockedResources)
	}
	// Blocked (denied) subresources must not also be counted as load failures.
	// favicon.ico is browser-auto-requested and may 404 on the demo page; it
	// is a legitimate failure but unrelated to the blocked external assets.
	nonFaviconFails := 0
	for _, r := range res.Resources {
		if r.Status == "failed" && !strings.Contains(r.URL, "favicon") {
			nonFaviconFails++
		}
	}
	if nonFaviconFails != 0 {
		t.Errorf("non-favicon resource_failures=%d want 0 (blocked resources are not load failures)", nonFaviconFails)
	}
	blockedRows, otherRows := 0, 0
	for _, r := range res.Resources {
		if r.Status == "blocked" {
			blockedRows++
			if r.BlockedReason == "" {
				t.Errorf("blocked row missing reason: %s", r.URL)
			}
		} else {
			otherRows++
		}
	}
	if blockedRows < 3 {
		t.Errorf("blocked waterfall rows=%d want >=3 (no double rows from id namespaces)", blockedRows)
	}
	if otherRows == 0 {
		t.Error("document/allowed resources missing from waterfall")
	}
}

func TestCollectRedirectLimit(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18206", "http://127.0.0.1:18206")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18206")

	_, err := Collect(context.Background(), "http://127.0.0.1:18206/redirect", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 20 * time.Second, SettleDelay: 500 * time.Millisecond,
	})
	if err == nil {
		t.Fatal("6-hop chain against a 5-hop limit must fail")
	}
	ce, ok := err.(*CollectError)
	if !ok || ce.Class != models.FailPolicy {
		t.Fatalf("err=%v want policy_violation", err)
	}
}

func TestCollectRedirectWithinLimitSucceeds(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18207", "http://127.0.0.1:18207")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18207")

	res, err := Collect(context.Background(), "http://127.0.0.1:18207/redirect?n=5", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 20 * time.Second, SettleDelay: 500 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("2-hop chain should succeed: %v", err)
	}
	if res.RedirectHops != 2 {
		t.Errorf("redirect hops=%d want 2", res.RedirectHops)
	}
	if res.FinalURL != "http://127.0.0.1:18207/" {
		t.Errorf("final url=%s", res.FinalURL)
	}
}

func TestCollectNavigationTimeout(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18208", "http://127.0.0.1:18208")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18208")

	start := time.Now()
	_, err := Collect(context.Background(), "http://127.0.0.1:18208/hang", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 4 * time.Second, SettleDelay: 500 * time.Millisecond,
	})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("hanging page must time out")
	}
	ce, ok := err.(*CollectError)
	if !ok || ce.Class != models.FailNavigationTimeout {
		t.Fatalf("err=%v want navigation_timeout", err)
	}
	if elapsed > 12*time.Second {
		t.Errorf("timeout took %v; browser resource likely not released fast enough", elapsed)
	}
}

func TestCollectPartialResourceFailure(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18209", "http://127.0.0.1:18209")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18209")

	res, err := Collect(context.Background(), "http://127.0.0.1:18209/missing", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 20 * time.Second, SettleDelay: 500 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("page load itself succeeds: %v", err)
	}
	if res.ResourceFailures < 2 {
		t.Errorf("resource failures=%d want >=2 (404 png+js)", res.ResourceFailures)
	}
	// Metrics must still be real.
	if m := metricByName(t, res, MetricFCP); m.Status != models.MetricCollected {
		t.Errorf("fcp status=%s even with partial subresource failure", m.Status)
	}
}

func TestCollectLongTasks(t *testing.T) {
	cleanup := startEnv(t, "127.0.0.1:18210", "http://127.0.0.1:18210")
	defer cleanup()
	c := checkerFor(t, "http://127.0.0.1:18210")

	res, err := Collect(context.Background(), "http://127.0.0.1:18210/longtask", Options{
		Viewport: models.ViewportDesktop, Checker: c, MaxRedirects: 5,
		NavTimeout: 20 * time.Second, SettleDelay: 1500 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	lt := metricByName(t, res, MetricLongTasks)
	if lt.Status != models.MetricCollected {
		t.Fatalf("longtask status=%s detail=%s", lt.Status, lt.Detail)
	}
}

func TestCollectRejectsNonHTTPAndNonWhitelisted(t *testing.T) {
	c := checkerFor(t, "http://127.0.0.1:18211")
	cases := map[string]string{
		"file:///etc/passwd":        models.FailPolicy,
		"http://evil.example.com/x": models.FailPolicy,
	}
	for u, wantClass := range cases {
		_, err := Collect(context.Background(), u, Options{
			Viewport: models.ViewportDesktop, Checker: c, NavTimeout: 5 * time.Second,
		})
		if err == nil {
			t.Errorf("%s: expected rejection", u)
			continue
		}
		ce, ok := err.(*CollectError)
		if !ok || ce.Class != wantClass {
			t.Errorf("%s: err=%v want class %s", u, err, wantClass)
		}
	}
}
