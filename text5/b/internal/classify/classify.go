// Package classify assigns each application a productivity category using
// an ordered, versioned set of local wildcard rules.
package classify

import (
	"regexp"
	"strings"
)

// Categories.
const (
	Productive   = "productive"
	Unproductive = "unproductive"
	Neutral      = "neutral"
)

// Rule is one wildcard classification rule.
type Rule struct {
	ID       int64
	Pattern  string
	Category string
	Priority int
}

// Classify returns the category of the first matching rule, ordered by
// priority (descending) then rule ID (ascending) so equal-priority rule
// sets always resolve the same way. Unmatched apps are neutral.
func Classify(rules []Rule, app string) string {
	best := -1
	for i := range rules {
		if !Match(rules[i].Pattern, app) {
			continue
		}
		if best == -1 ||
			rules[i].Priority > rules[best].Priority ||
			(rules[i].Priority == rules[best].Priority && rules[i].ID < rules[best].ID) {
			best = i
		}
	}
	if best == -1 {
		return Neutral
	}
	return rules[best].Category
}

// Match reports whether the wildcard pattern matches s, case-insensitively.
// '*' matches any run of characters, '?' exactly one; everything else is
// literal. The whole string must match.
func Match(pattern, s string) bool {
	var b strings.Builder
	b.WriteString("(?i)^")
	for _, r := range pattern {
		switch r {
		case '*':
			b.WriteString(".*")
		case '?':
			b.WriteString(".")
		default:
			b.WriteString(regexp.QuoteMeta(string(r)))
		}
	}
	b.WriteString("$")
	ok, err := regexp.MatchString(b.String(), s)
	return err == nil && ok
}
