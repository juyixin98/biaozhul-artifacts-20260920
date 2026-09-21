package policy

import (
	"testing"

	"sitevitals/internal/models"
)

func TestChecker(t *testing.T) {
	sites := []models.Site{
		{ID: 1, SchemeHost: "http://127.0.0.1:8093", Enabled: true},
		{ID: 2, SchemeHost: "https://intra.example.com", Enabled: true},
	}
	rules := []models.AllowedURL{
		{ID: 1, SiteID: 1, URLPattern: "http://127.0.0.1:8093/*", Enabled: true},
		{ID: 2, SiteID: 2, URLPattern: "https://intra.example.com/report", Enabled: true},
	}
	c, err := Compile(sites, rules)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	allow := []string{
		"http://127.0.0.1:8093/",
		"http://127.0.0.1:8093/asset/app.js?x=1",
		"https://intra.example.com/report",
	}
	for _, u := range allow {
		if v := c.Check(u); v != nil {
			t.Errorf("expected allowed %q, got %v", u, v)
		}
	}

	deny := []struct {
		url  string
		code string
	}{
		{"file:///etc/passwd", ViolationNonHTTP},
		{"ftp://127.0.0.1/x", ViolationNonHTTP},
		{"javascript:alert(1)", ViolationNonHTTP},
		{"data:text/html,<h1>x</h1>", ViolationNonHTTP},
		{"http://evil.example.com/", ViolationNotAllowed},
		{"http://127.0.0.1:9000/", ViolationNotAllowed},
		{"https://intra.example.com/admin", ViolationNotAllowed}, // exact rule only
		{"https://intra.example.com.report.evil.com/", ViolationNotAllowed},
	}
	for _, d := range deny {
		v := c.Check(d.url)
		if v == nil {
			t.Errorf("expected denial for %q", d.url)
			continue
		}
		if v.Code != d.code {
			t.Errorf("%q: got code %s want %s", d.url, v.Code, d.code)
		}
	}
}

func TestCompileRejectsMismatchedHost(t *testing.T) {
	sites := []models.Site{{ID: 1, SchemeHost: "http://a.test", Enabled: true}}
	rules := []models.AllowedURL{{SiteID: 1, URLPattern: "http://b.test/x", Enabled: true}}
	if _, err := Compile(sites, rules); err == nil {
		t.Fatal("expected rule host mismatch to be rejected")
	}
}

func TestRedirectGuard(t *testing.T) {
	g := NewRedirectGuard(5)
	for i := 1; i <= 5; i++ {
		if v := g.Hop("http://x"); v != nil {
			t.Fatalf("hop %d should be allowed: %v", i, v)
		}
	}
	if v := g.Hop("http://x"); v == nil || v.Code != ViolationRedirects {
		t.Fatalf("hop 6 must be rejected, got %v", v)
	}
	if g.Hops() != 6 {
		t.Fatalf("hops=%d want 6", g.Hops())
	}
}
