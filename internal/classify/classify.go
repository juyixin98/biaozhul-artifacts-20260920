package classify

import (
	"regexp"
	"strings"

	"desklens/internal/glob"
	"desklens/internal/repo"
)

const (
	Productive    = "productive"
	NonProductive = "non_productive"
	Neutral       = "neutral"
)

// compiledRule is a local glob anchored at both ends, matched
// case-insensitively. '*' is the only wildcard (any run of characters),
// following the documented local wildcard convention.
type compiledRule struct {
	ruleID   int64
	category string
	priority int
	re       *regexp.Regexp
}

// Matcher holds one frozen classification version. Because the matcher is
// built once per ingestion batch from the rules read inside that batch's
// transaction, classification is stable and version-stamped.
type Matcher struct {
	version int
	rules   []compiledRule
}

func NewMatcher(c repo.Classification) (*Matcher, error) {
	m := &Matcher{version: c.Version}
	for _, r := range c.Rules {
		re, err := glob.Compile(r.Pattern)
		if err != nil {
			return nil, err
		}
		m.rules = append(m.rules, compiledRule{
			ruleID:   r.RuleID,
			category: r.Category,
			priority: r.Priority,
			re:       re,
		})
	}
	// Defense in depth: repo ordering is priority DESC then rule_id ASC.
	for i := 1; i < len(m.rules); i++ {
		for j := i; j > 0; j-- {
			a, b := m.rules[j-1], m.rules[j]
			if a.priority < b.priority || (a.priority == b.priority && a.ruleID > b.ruleID) {
				m.rules[j-1], m.rules[j] = b, a
			} else {
				break
			}
		}
	}
	return m, nil
}

func (m *Matcher) Version() int { return m.version }

// Match returns the category of the first matching rule in stable order.
// Seed data always includes a catch-all '*' neutral rule, but if an operator
// publishes a version without one, unmatched apps default to neutral rather
// than being dropped.
func (m *Matcher) Match(appName string) string {
	name := strings.ToLower(appName)
	for _, r := range m.rules {
		if r.re.MatchString(name) {
			return r.category
		}
	}
	return Neutral
}
