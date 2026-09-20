// Package classify assigns apps to productivity categories using local
// wildcard rules. Matching is deterministic: highest priority wins, ties are
// broken by the lowest rule ID, so the same rule set always yields the same
// category.
package classify

import "strings"

type Category string

const (
	Productive   Category = "productive"
	Unproductive Category = "unproductive"
	Neutral      Category = "neutral"
)

type Rule struct {
	ID       int64
	Pattern  string
	Category Category
	Priority int
}

// Match returns the category for an app name. Rules are compared by
// (priority DESC, id ASC); the first matching rule in that order wins.
// Apps matching nothing are Neutral.
func Match(rules []Rule, app string) Category {
	var best *Rule
	for i := range rules {
		r := &rules[i]
		if !GlobMatch(r.Pattern, app) {
			continue
		}
		if best == nil ||
			r.Priority > best.Priority ||
			(r.Priority == best.Priority && r.ID < best.ID) {
			best = r
		}
	}
	if best == nil {
		return Neutral
	}
	return best.Category
}

// GlobMatch reports whether name matches a wildcard pattern with '*'
// (any run of characters) and '?' (exactly one character). Matching is
// case-insensitive.
func GlobMatch(pattern, name string) bool {
	p := []rune(strings.ToLower(pattern))
	n := []rune(strings.ToLower(name))

	pi, ni := 0, 0
	star, starNext := -1, 0 // index of last '*' in p, and n-position to retry from
	for ni < len(n) {
		if pi < len(p) && (p[pi] == '?' || p[pi] == n[ni]) {
			pi++
			ni++
			continue
		}
		if pi < len(p) && p[pi] == '*' {
			star = pi
			starNext = ni
			pi++
			continue
		}
		if star >= 0 {
			pi = star + 1
			starNext++
			ni = starNext
			continue
		}
		return false
	}
	for pi < len(p) && p[pi] == '*' {
		pi++
	}
	return pi == len(p)
}
