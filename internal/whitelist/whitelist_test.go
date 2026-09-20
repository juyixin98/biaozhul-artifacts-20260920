package whitelist_test

import (
	"strings"
	"testing"

	"sitevitals/internal/models"
	"sitevitals/internal/whitelist"
)

func sitesForTest() []models.Site {
	return []models.Site{
		{ID: 1, Name: "demo", Origin: "http://demo.local:8090", PathPrefix: "/", Enabled: true},
		{ID: 2, Name: "intranet", Origin: "http://intranet.example.com", PathPrefix: "/apps/", Enabled: true},
		{ID: 3, Name: "wildcard", Origin: "https://*.corp.example.net", PathPrefix: "/", Enabled: true},
		{ID: 4, Name: "disabled", Origin: "http://off.example.com", PathPrefix: "/", Enabled: false},
	}
}

func TestParseHTTPURL_RejectsNonHTTP(t *testing.T) {
	bad := []string{
		"file:///etc/passwd",
		"FILE:///etc/passwd",
		"data:text/html,<script>",
		"javascript:alert(1)",
		"ftp://host/file",
		"chrome://settings",
		"view-source:http://demo.local/",
		"",
		"not a url",
		"//demo.local/", // scheme-relative: no http scheme
	}
	for _, raw := range bad {
		if _, err := whitelist.ParseHTTPURL(raw); err == nil {
			t.Errorf("ParseHTTPURL(%q) accepted, want rejection", raw)
		}
	}
	good := []string{"http://demo.local/", "https://demo.local/a?b=1", "HTTP://demo.local:8080/x"}
	for _, raw := range good {
		if _, err := whitelist.ParseHTTPURL(raw); err != nil {
			t.Errorf("ParseHTTPURL(%q) rejected: %v", raw, err)
		}
	}
}

func TestMatcher_Whitelist(t *testing.T) {
	m := whitelist.NewMatcher(sitesForTest())
	cases := []struct {
		raw   string
		allow bool
	}{
		{"http://demo.local:8090/", true},
		{"http://demo.local:8090/slow?x=1", true},
		{"http://demo.local:9090/", false},      // port mismatch
		{"http://intranet.example.com/", false}, // prefix /apps/ only
		{"http://intranet.example.com/apps/board", true},
		{"http://intranet.example.com/apps/../etc", false}, // cleaned outside prefix
		{"https://host.corp.example.net/", true},
		{"https://corp.example.net/", false}, // wildcard needs one label
		{"http://off.example.com/", false},   // disabled entry
		{"http://evil.invalid/", false},
		{"https://demo.local:8090/", false}, // scheme mismatch
		{"http://DEMO.LOCAL:8090/A/", true}, // host case-insensitive
	}
	for _, c := range cases {
		_, err := m.ValidateTarget(c.raw)
		got := err == nil
		if got != c.allow {
			t.Errorf("ValidateTarget(%q) = %v (err=%v), want %v", c.raw, got, err, c.allow)
		}
	}
}

func TestMatcher_NonHTTPErrorMentionsProtocol(t *testing.T) {
	m := whitelist.NewMatcher(sitesForTest())
	_, err := m.ValidateTarget("file:///etc/passwd")
	if err == nil || !strings.Contains(err.Error(), "non-HTTP") {
		t.Fatalf("error should mention non-HTTP protocol, got: %v", err)
	}
}

func TestNormalize(t *testing.T) {
	cases := map[string]string{
		"http://demo.local:80/a/":     "http://demo.local/a/",
		"http://demo.local/a/../b":    "http://demo.local/b",
		"HTTP://Demo.Local/x?z=2&a=1": "http://demo.local/x?a=1&z=2",
		"http://demo.local/x#frag":    "http://demo.local/x",
		"https://demo.local:443/":     "https://demo.local/",
	}
	for in, want := range cases {
		got, err := whitelist.NormalizeRaw(in)
		if err != nil {
			t.Errorf("normalize %q: %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("normalize %q = %q, want %q", in, got, want)
		}
	}
}

func TestViewports(t *testing.T) {
	for _, vp := range []models.Viewport{models.ViewportMobile, models.ViewportTablet, models.ViewportDesktop} {
		if !vp.Valid() {
			t.Fatalf("viewport %s invalid", vp)
		}
		p, err := whitelist.Profile(vp)
		if err != nil || p.Width <= 0 || p.Height <= 0 {
			t.Fatalf("profile %s: %+v err=%v", vp, p, err)
		}
	}
	if models.Viewport("tv").Valid() {
		t.Fatal("tv must not be a valid viewport")
	}
}
