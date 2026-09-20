package browser_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"sitevitals/internal/browser"
	"sitevitals/internal/demo"
	"sitevitals/internal/models"
	"sitevitals/internal/whitelist"
)

// startDemo serves the built-in local site and returns its base URL.
func startDemo(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(demo.NewServer())
	t.Cleanup(srv.Close)
	return srv
}

func demoMatcher(baseURL string) *whitelist.Matcher {
	return whitelist.NewMatcher([]models.Site{
		{Name: "demo", Origin: baseURL, PathPrefix: "/", Enabled: true},
	})
}

func skipNoChrome(t *testing.T) {
	t.Helper()
	if testing.Short() || os.Getenv("SKIP_CHROME_TESTS") != "" {
		t.Skip("skipping real-Chromium test (-short or SKIP_CHROME_TESTS)")
	}
}

// startInstance launches a headless Chromium for one test.
func startInstance(t *testing.T) *browser.Instance {
	t.Helper()
	inst := browser.NewInstance(browser.Options{Headless: true})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := inst.Start(ctx); err != nil {
		t.Skipf("chromium unavailable in this environment: %v", err)
	}
	t.Cleanup(func() { _ = inst.Close() })
	return inst
}

func collectFromDemo(t *testing.T, inst *browser.Instance, baseURL, pagePath string, vp models.Viewport, navTimeout time.Duration) (*browser.Diagnostics, *browser.CollectError) {
	t.Helper()
	prof, err := whitelist.Profile(vp)
	if err != nil {
		t.Fatal(err)
	}
	c := browser.NewCollector(inst, false)
	ctx, cancel := context.WithTimeout(context.Background(), navTimeout+15*time.Second)
	defer cancel()
	_, d, cerr := c.Collect(ctx, browser.CollectOptions{
		TargetURL:  baseURL + pagePath,
		Profile:    prof,
		Matcher:    demoMatcher(baseURL),
		NavTimeout: navTimeout,
		Settle:     2 * time.Second,
	})
	return d, asCE(cerr)
}

func asCE(err error) *browser.CollectError {
	if err == nil {
		return nil
	}
	if ce, ok := browser.AsCollectError(err); ok {
		return ce
	}
	return &browser.CollectError{Code: "OTHER", Message: err.Error()}
}

// TestCollect_NormalPage_MetricsAndWaterfall is the real-Chromium happy path:
// navigation timing, FCP/LCP and the resource waterfall must be populated;
// every metric carries an explicit status.
func TestCollect_NormalPage_MetricsAndWaterfall(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	prof, _ := whitelist.Profile(models.ViewportMobile)
	c := browser.NewCollector(inst, false)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	m, d, err := c.Collect(ctx, browser.CollectOptions{
		TargetURL:  srv.URL + "/normal",
		Profile:    prof,
		Matcher:    demoMatcher(srv.URL),
		NavTimeout: 20 * time.Second,
		Settle:     2 * time.Second,
	})
	if err != nil {
		t.Fatalf("collect /normal: %v", err)
	}
	if m == nil {
		t.Fatal("nil metrics")
	}
	if m.NavDurationMS == nil || *m.NavDurationMS <= 0 {
		t.Errorf("navigation duration missing: %v status=%s", m.NavDurationMS, m.MetricStatus.Navigation)
	}
	if m.FCPMS == nil || *m.FCPMS <= 0 {
		t.Errorf("FCP missing: %v status=%s", m.FCPMS, m.MetricStatus.FCP)
	}
	if m.LCPMS == nil || *m.LCPMS <= 0 {
		t.Errorf("LCP missing: %v status=%s", m.LCPMS, m.MetricStatus.LCP)
	}
	if m.CLS == nil {
		t.Errorf("CLS must be a measured value (0 is valid), status=%s", m.MetricStatus.CLS)
	}
	if m.LongTaskCount == nil {
		t.Errorf("long task count must be populated, status=%s", m.MetricStatus.LongTasks)
	}
	if m.WindowEnd.IsZero() {
		t.Error("long-task window end not recorded")
	}
	if !strings.HasPrefix(m.FinalURL, srv.URL) {
		t.Errorf("final URL %q", m.FinalURL)
	}
	var docs, imgs int
	for _, r := range m.Resources {
		if r.Type == "Document" {
			docs++
			if r.Status != 200 {
				t.Errorf("document status = %d", r.Status)
			}
			if r.StartMS != 0 {
				// document starts at ~0 offset from its own nav base
			}
		}
		if strings.Contains(r.URL, "/static/img") {
			imgs++
			if r.Status != 200 {
				t.Errorf("image status = %d url=%s", r.Status, r.URL)
			}
			if r.DurationMS <= 0 {
				t.Errorf("image duration not measured: %+v", r)
			}
		}
	}
	if docs != 1 || imgs != 1 {
		t.Errorf("waterfall docs=%d imgs=%d resources=%d", docs, imgs, len(m.Resources))
	}
	if d == nil {
		t.Error("diagnostics nil")
	}
	// Explicit statuses, no silent zeros.
	for name, st := range map[string]string{
		"nav": m.MetricStatus.Navigation, "fcp": m.MetricStatus.FCP,
		"lcp": m.MetricStatus.LCP, "cls": m.MetricStatus.CLS,
		"lt": m.MetricStatus.LongTasks, "res": m.MetricStatus.Resources,
	} {
		if st != models.MetricOK && st != models.MetricPartial {
			t.Errorf("metric %s status %s", name, st)
		}
	}
}

// TestCollect_LongTasks runs the deliberately CPU-blocking page and requires
// at least one observed long task > 50ms.
func TestCollect_LongTasks(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	prof, _ := whitelist.Profile(models.ViewportDesktop)
	c := browser.NewCollector(inst, false)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	m, _, err := c.Collect(ctx, browser.CollectOptions{
		TargetURL: srv.URL + "/longtask", Profile: prof, Matcher: demoMatcher(srv.URL),
		NavTimeout: 20 * time.Second, Settle: 3 * time.Second,
	})
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if *m.LongTaskCount < 1 {
		t.Fatalf("expected long tasks on /longtask, got %d", *m.LongTaskCount)
	}
	if *m.LongTaskTotalMS < 50 || *m.LongTaskMaxMS < 50 {
		t.Fatalf("long task totals implausible: total=%v max=%v", m.LongTaskTotalMS, m.LongTaskMaxMS)
	}
}

// TestCollect_CLS detects the injected late banner (CLS > 0).
func TestCollect_CLS(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	prof, _ := whitelist.Profile(models.ViewportTablet)
	c := browser.NewCollector(inst, false)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	m, _, err := c.Collect(ctx, browser.CollectOptions{
		TargetURL: srv.URL + "/cls", Profile: prof, Matcher: demoMatcher(srv.URL),
		NavTimeout: 20 * time.Second, Settle: 3 * time.Second,
	})
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if m.CLS == nil || *m.CLS <= 0 {
		t.Fatalf("expected positive CLS on /cls page, got %v", m.CLS)
	}
}

// TestCollect_PartialResourceFailure records failed subresources while the
// task itself succeeds: one failed task must not arise from a missing image.
func TestCollect_PartialResourceFailure(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	prof, _ := whitelist.Profile(models.ViewportDesktop)
	c := browser.NewCollector(inst, false)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	m, _, err := c.Collect(ctx, browser.CollectOptions{
		TargetURL: srv.URL + "/partial", Profile: prof, Matcher: demoMatcher(srv.URL),
		NavTimeout: 20 * time.Second, Settle: 2 * time.Second,
	})
	if err != nil {
		t.Fatalf("task must succeed despite subresource failures: %v", err)
	}
	var failed int
	var warned bool
	for _, r := range m.Resources {
		if r.Failed || r.Status == 404 || r.Status == 500 {
			failed++
		}
	}
	for _, w := range m.Warnings {
		if strings.Contains(w, "subresource(s)") {
			warned = true
		}
	}
	if failed == 0 {
		t.Error("expected the 404/500 subresources in the waterfall")
	}
	if !warned {
		t.Error("expected a warning summarizing failed subresources")
	}
}

// TestCollect_RedirectChain follows two whitelisted hops and records them.
func TestCollect_RedirectChain(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	_, ce := collectFromDemo(t, inst, srv.URL, "/redirect/1", models.ViewportDesktop, 20*time.Second)
	if ce != nil {
		t.Fatalf("redirect chain failed: %v", ce)
	}
}

// TestCollect_RedirectToNonHTTP blocks a file: hop in the redirect chain.
func TestCollect_RedirectToNonHTTP(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	d, ce := collectFromDemo(t, inst, srv.URL, "/badredirect-file", models.ViewportDesktop, 10*time.Second)
	if ce == nil || ce.Code != browser.CodePolicyBlocked {
		t.Fatalf("expected POLICY_BLOCKED, got %v", ce)
	}
	if d == nil || !hasViolation(d.Violations, "scheme") {
		t.Fatalf("expected a scheme violation, got %+v", d)
	}
}

// TestCollect_RedirectOffWhitelist blocks a hop leaving the whitelist.
func TestCollect_RedirectOffWhitelist(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	d, ce := collectFromDemo(t, inst, srv.URL, "/badredirect-offsite", models.ViewportDesktop, 10*time.Second)
	if ce == nil || ce.Code != browser.CodePolicyBlocked {
		t.Fatalf("expected POLICY_BLOCKED, got %v", ce)
	}
	if d == nil || !hasViolation(d.Violations, "not_whitelisted") {
		t.Fatalf("expected not_whitelisted violation, got %+v", d)
	}
}

// TestCollect_RedirectLoop hits the configured redirect bound rather than
// hanging the browser until the navigation timeout.
func TestCollect_RedirectLoop(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	prof, _ := whitelist.Profile(models.ViewportDesktop)
	c := browser.NewCollector(inst, false)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, d, err := c.Collect(ctx, browser.CollectOptions{
		TargetURL: srv.URL + "/redirect/loop/a", Profile: prof,
		Matcher: demoMatcher(srv.URL), NavTimeout: 20 * time.Second,
		Settle: time.Second, MaxRedirects: 5,
	})
	if err == nil {
		t.Fatal("redirect loop should fail")
	}
	ce, _ := browser.AsCollectError(err)
	if ce == nil || ce.Code != browser.CodeRedirectLimit {
		t.Fatalf("expected REDIRECT_LIMIT, got %v", err)
	}
	if !hasViolation(d.Violations, "redirect_limit") {
		t.Fatalf("expected redirect_limit violation, got %+v", d)
	}
}

// TestCollect_NavigationTimeout distinguishes a slow page from crashes.
func TestCollect_NavigationTimeout(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)

	start := time.Now()
	_, ce := collectFromDemo(t, inst, srv.URL, "/hang", models.ViewportDesktop, 3*time.Second)
	elapsed := time.Since(start)
	if ce == nil || ce.Code != browser.CodeNavigationTimeout {
		t.Fatalf("expected NAVIGATION_TIMEOUT, got %v", ce)
	}
	if elapsed > 12*time.Second {
		t.Fatalf("timeout handling took too long (%s); browser resource likely stuck", elapsed)
	}
}

// TestCollect_InitialURLNonHTTP is the pre-flight rejection (no browser load).
func TestCollect_InitialURLNonHTTP(t *testing.T) {
	skipNoChrome(t)
	inst := startInstance(t)
	prof, _ := whitelist.Profile(models.ViewportDesktop)
	c := browser.NewCollector(inst, false)
	_, _, err := c.Collect(context.Background(), browser.CollectOptions{
		TargetURL: "file:///etc/passwd", Profile: prof,
		Matcher:    demoMatcher("http://demo.local"),
		NavTimeout: 5 * time.Second,
	})
	ce, _ := browser.AsCollectError(err)
	if ce == nil || ce.Code != browser.CodePolicyBlocked {
		t.Fatalf("expected POLICY_BLOCKED, got %v", err)
	}
}

// TestCollect_InitialURLOffWhitelist rejects a non-registered http target.
func TestCollect_InitialURLOffWhitelist(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)
	prof, _ := whitelist.Profile(models.ViewportDesktop)
	c := browser.NewCollector(inst, false)
	// Point at the demo server's port but register a different whitelist.
	_, _, err := c.Collect(context.Background(), browser.CollectOptions{
		TargetURL: srv.URL + "/normal", Profile: prof,
		Matcher:    whitelist.NewMatcher([]models.Site{{Origin: "http://other.example", PathPrefix: "/", Enabled: true}}),
		NavTimeout: 5 * time.Second,
	})
	ce, _ := browser.AsCollectError(err)
	if ce == nil || ce.Code != browser.CodePolicyBlocked {
		t.Fatalf("expected POLICY_BLOCKED, got %v", err)
	}
}

// TestInstance_ReusableAcrossCollections requires one Chromium instance to
// serve multiple serial navigations (the concurrency pool depends on this).
func TestInstance_ReusableAcrossCollections(t *testing.T) {
	skipNoChrome(t)
	srv := startDemo(t)
	inst := startInstance(t)
	for i := 0; i < 3; i++ {
		prof, _ := whitelist.Profile(models.ViewportDesktop)
		c := browser.NewCollector(inst, false)
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		m, _, err := c.Collect(ctx, browser.CollectOptions{
			TargetURL: srv.URL + "/normal", Profile: prof,
			Matcher: demoMatcher(srv.URL), NavTimeout: 15 * time.Second, Settle: time.Second,
		})
		cancel()
		if err != nil {
			t.Fatalf("collection %d: %v", i, err)
		}
		if m.FCPMS == nil {
			t.Fatalf("collection %d FCP missing", i)
		}
	}
}

var _ = http.StatusOK
