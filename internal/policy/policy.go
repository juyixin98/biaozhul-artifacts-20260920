// Package policy enforces the allow-list: only http/https URLs matching a
// registered exact-or-prefix rule may be navigated to or loaded as a
// subresource. Redirects are checked hop by hop.
package policy

import (
	"fmt"
	"net/url"
	"strings"

	"sitevitals/internal/models"
)

// Violation codes.
const (
	ViolationNonHTTP    = "non_http_scheme"
	ViolationNotAllowed = "not_in_allowlist"
	ViolationRedirects  = "too_many_redirects"
)

// Violation describes a rejected request.
type Violation struct {
	Code   string `json:"code"`
	Reason string `json:"reason"`
	URL    string `json:"url"`
}

func (e *Violation) Error() string { return fmt.Sprintf("%s: %s (%s)", e.Code, e.Reason, e.URL) }

// pattern is a compiled allow-list rule.
type pattern struct {
	raw    string
	scheme string
	host   string
	// prefix is matched against requestURI (path+query); empty means "/".
	prefix string
	exact  bool
}

// Checker holds the compiled allow-list.
type Checker struct {
	patterns []pattern
}

// Compile builds a Checker from enabled sites and enabled URL rules.
func Compile(sites []models.Site, rules []models.AllowedURL) (*Checker, error) {
	enabledSites := map[uint64]string{}
	for _, s := range sites {
		if s.Enabled {
			u, err := normalizeOrigin(s.SchemeHost)
			if err != nil {
				return nil, fmt.Errorf("site %d origin %q: %w", s.ID, s.SchemeHost, err)
			}
			enabledSites[s.ID] = u
		}
	}
	c := &Checker{}
	for _, r := range rules {
		if !r.Enabled {
			continue
		}
		origin, ok := enabledSites[r.SiteID]
		if !ok {
			continue
		}
		p, err := compileRule(origin, r.URLPattern)
		if err != nil {
			return nil, fmt.Errorf("rule %d %q: %w", r.ID, r.URLPattern, err)
		}
		c.patterns = append(c.patterns, p)
	}
	return c, nil
}

func normalizeOrigin(raw string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return "", err
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", &Violation{Code: ViolationNonHTTP, Reason: "site origin must be http/https", URL: raw}
	}
	if u.Host == "" {
		return "", fmt.Errorf("missing host")
	}
	return strings.ToLower(u.Scheme) + "://" + strings.ToLower(u.Host), nil
}

func compileRule(origin, raw string) (pattern, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return pattern{}, err
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return pattern{}, &Violation{Code: ViolationNonHTTP, Reason: "rule must be http/https", URL: raw}
	}
	p := pattern{raw: raw, scheme: strings.ToLower(u.Scheme), host: strings.ToLower(u.Host)}
	if origin != p.scheme+"://"+p.host {
		return pattern{}, fmt.Errorf("rule host %q does not belong to site origin %q", p.host, origin)
	}
	uri := u.RequestURI()
	if strings.HasSuffix(uri, "*") {
		p.prefix = strings.TrimSuffix(uri, "*")
		p.exact = false
	} else {
		p.prefix = uri
		p.exact = true
	}
	return p, nil
}

// Check validates one absolute URL (navigation target or subresource).
func (c *Checker) Check(rawURL string) *Violation {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return &Violation{Code: ViolationNotAllowed, Reason: "unparseable URL", URL: rawURL}
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return &Violation{Code: ViolationNonHTTP, Reason: "only http/https is permitted", URL: rawURL}
	}
	if u.Host == "" {
		return &Violation{Code: ViolationNotAllowed, Reason: "missing host", URL: rawURL}
	}
	reqHost := strings.ToLower(u.Host)
	reqScheme := strings.ToLower(u.Scheme)
	reqURI := u.RequestURI()
	for _, p := range c.patterns {
		if p.scheme != reqScheme || p.host != reqHost {
			continue
		}
		if p.exact {
			if reqURI == p.prefix {
				return nil
			}
			continue
		}
		if strings.HasPrefix(reqURI, p.prefix) {
			return nil
		}
	}
	return &Violation{Code: ViolationNotAllowed, Reason: "URL matches no enabled allow-list rule", URL: rawURL}
}

// RedirectGuard enforces the redirect hop limit during a navigation.
type RedirectGuard struct {
	maxHops int
	hops    int
}

// NewRedirectGuard creates a guard; maxHops is the number of redirect responses
// permitted before the final document (e.g. 5 tolerates a 5-hop chain).
func NewRedirectGuard(maxHops int) *RedirectGuard { return &RedirectGuard{maxHops: maxHops} }

// Hop records a redirect response. It returns a Violation when the limit is exceeded.
func (g *RedirectGuard) Hop(redirectedTo string) *Violation {
	g.hops++
	if g.hops > g.maxHops {
		return &Violation{
			Code:   ViolationRedirects,
			Reason: fmt.Sprintf("redirect limit of %d exceeded while following to %s", g.maxHops, redirectedTo),
			URL:    redirectedTo,
		}
	}
	return nil
}

// Hops returns the number of redirect responses seen so far.
func (g *RedirectGuard) Hops() int { return g.hops }

// IsRedirectStatus reports whether an HTTP status is a redirect (3xx with Location).
func IsRedirectStatus(status int64) bool { return status >= 300 && status < 400 }
