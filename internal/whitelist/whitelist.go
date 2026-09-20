package whitelist

import (
	"fmt"
	"net"
	"net/url"
	"path"
	"strings"

	"sitevitals/internal/models"
)

// DeviceProfile describes the emulated device for one viewport.
type DeviceProfile struct {
	Width  int64
	Height int64
	Scale  float64
	Mobile bool
	UA     string
}

// ViewportProfiles are the three supported emulated viewports. UA strings
// identify the device class; layout viewport matches a real phone/tablet.
var ViewportProfiles = map[models.Viewport]DeviceProfile{
	models.ViewportMobile: {
		Width: 390, Height: 844, Scale: 3, Mobile: true,
		UA: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
	},
	models.ViewportTablet: {
		Width: 820, Height: 1180, Scale: 2, Mobile: true,
		UA: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
	},
	models.ViewportDesktop: {
		Width: 1366, Height: 768, Scale: 1, Mobile: false,
		UA: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
	},
}

// Profile returns the device profile for a viewport.
func Profile(v models.Viewport) (DeviceProfile, error) {
	if p, ok := ViewportProfiles[v]; ok {
		return p, nil
	}
	return DeviceProfile{}, fmt.Errorf("unknown viewport %q", v)
}

// Matcher evaluates URLs against the set of enabled whitelist entries.
type Matcher struct {
	entries []models.Site
}

// NewMatcher builds a matcher from site entries (callers typically pass only
// enabled ones; disabled entries are filtered here too).
func NewMatcher(sites []models.Site) *Matcher {
	m := &Matcher{}
	for _, s := range sites {
		if s.Enabled {
			m.entries = append(m.entries, s)
		}
	}
	return m
}

// ValidateTarget parses a task URL and rejects anything that is not
// http/https or that falls outside the whitelist. It returns the normalized
// URL (lower-case scheme/host, cleaned path) used as the comparison key.
func (m *Matcher) ValidateTarget(raw string) (string, error) {
	u, err := ParseHTTPURL(raw)
	if err != nil {
		return "", err
	}
	if !m.Allows(u) {
		return "", fmt.Errorf("%w: %s", ErrNotWhitelisted, u.String())
	}
	return Normalize(u), nil
}

// ErrNotWhitelisted is returned for hosts/paths no site entry covers.
var ErrNotWhitelisted = fmt.Errorf("address is not in the site whitelist")

// Allows reports whether an already-parsed URL matches an enabled entry.
func (m *Matcher) Allows(u *url.URL) bool {
	host := strings.ToLower(u.Hostname())
	requestPath := u.EscapedPath()
	if requestPath == "" {
		requestPath = "/"
	}
	for _, e := range m.entries {
		eu, err := url.Parse(e.Origin)
		if err != nil {
			continue
		}
		if strings.ToLower(eu.Scheme) != u.Scheme {
			continue
		}
		if !hostMatch(strings.ToLower(eu.Hostname()), host) {
			continue
		}
		if !portMatch(eu.Port(), u.Port()) {
			continue
		}
		prefix := e.PathPrefix
		if prefix == "" {
			prefix = "/"
		}
		if pathPrefixMatch(prefix, requestPath) {
			return true
		}
	}
	return false
}

// hostMatch implements exact host match with one wildcard label only at the
// leftmost position (e.g. "*.internal.example").
func hostMatch(pattern, host string) bool {
	if pattern == host {
		return true
	}
	if strings.HasPrefix(pattern, "*.") {
		suffix := pattern[1:] // ".example.com"
		if strings.HasSuffix(host, suffix) && len(host) > len(suffix) {
			return true
		}
	}
	return false
}

// defaultPort returns the implicit port for a scheme, if any.
func defaultPort(scheme string) string {
	switch scheme {
	case "http":
		return "80"
	case "https":
		return "443"
	}
	return ""
}

func portMatch(want, got string) bool {
	if want == got {
		return true
	}
	if got == "" && want == "" {
		return true
	}
	return false
}

// pathPrefixMatch reports whether requestPath is under prefix.
func pathPrefixMatch(prefix, requestPath string) bool {
	prefix = path.Clean("/" + prefix)
	if prefix == "/" {
		return true
	}
	rp := path.Clean("/" + requestPath)
	if rp == prefix {
		return true
	}
	return strings.HasPrefix(rp, prefix+"/")
}

// ParseHTTPURL parses raw and only accepts http/https URLs with a host.
// Non-HTTP schemes (file:, data:, javascript:, ftp:…) and opaque inputs are
// rejected — the collector must never load them.
func ParseHTTPURL(raw string) (*url.URL, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, fmt.Errorf("empty URL")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("invalid URL %q: %w", raw, err)
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" {
		return nil, fmt.Errorf("non-HTTP protocol %q rejected for %q", u.Scheme, raw)
	}
	if u.Host == "" || u.Hostname() == "" {
		return nil, fmt.Errorf("URL %q has no host", raw)
	}
	if strings.Contains(u.Hostname(), "..") {
		// url.Parse already normalizes most of this, but be explicit.
		return nil, fmt.Errorf("invalid host in %q", raw)
	}
	return u, nil
}

// Normalize returns the canonical comparison key: lower scheme/host, explicit
// default port stripped, cleaned path, sorted query, no fragment.
func Normalize(u *url.URL) string {
	scheme := strings.ToLower(u.Scheme)
	host := strings.ToLower(u.Hostname())
	port := u.Port()
	if port == defaultPort(scheme) {
		port = ""
	}
	hostport := host
	if port != "" {
		hostport = net.JoinHostPort(host, port)
	}
	p := path.Clean("/" + u.EscapedPath())
	if strings.HasSuffix(u.EscapedPath(), "/") && !strings.HasSuffix(p, "/") {
		p += "/"
	}
	q := u.Query()
	return (&url.URL{Scheme: scheme, Host: hostport, Path: p, RawQuery: q.Encode()}).String()
}

// NormalizeRaw is a convenience wrapper returning an error for bad schemes.
func NormalizeRaw(raw string) (string, error) {
	u, err := ParseHTTPURL(raw)
	if err != nil {
		return "", err
	}
	return Normalize(u), nil
}
