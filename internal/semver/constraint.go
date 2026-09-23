package semver

import (
	"fmt"
	"strings"
)

// Constraint is a parsed version constraint: a disjunction (||) of
// comparator sets; each set is a conjunction of comparators.
//
// Supported syntax (simplified npm-style):
//
//	1.2.3            exact
//	>=1.2.0 <2.0.0   comparator set (space separated, AND)
//	^1.2.3           >=1.2.3 <2.0.0        (caret; 0.x rules per npm)
//	~1.2.3           >=1.2.3 <1.3.0        (tilde)
//	1.2.x / 1.2.*    >=1.2.0 <1.3.0        (wildcards, also 1.x, *)
//	>=1.0.0 || >=2.0.0   union
//	"" / "*"         any (non-prerelease)
//
// Prerelease rule (npm-style, simplified): a comparator set that does not
// mention a prerelease only matches release versions. If any comparator in
// the set carries a prerelease, prerelease versions sharing that
// comparator's [major,minor,patch] tuple are also eligible.
type Constraint struct {
	sets []comparatorSet
	raw  string
}

type comparator struct {
	op  string // =, !=, >, >=, <, <=
	ver Version
}

type comparatorSet struct {
	comps []comparator
	// preTuples are the [major,minor,patch] triples mentioned with a
	// prerelease in this set; prerelease versions of these tuples may match.
	preTuples [][3]int
}

// ParseConstraint parses a constraint string.
func ParseConstraint(s string) (*Constraint, error) {
	raw := s
	s = strings.TrimSpace(s)
	c := &Constraint{raw: raw}
	if s == "" || s == "*" || s == "x" || s == "X" {
		c.sets = []comparatorSet{{}}
		return c, nil
	}
	for _, alt := range strings.Split(s, "||") {
		alt = strings.TrimSpace(alt)
		if alt == "" {
			return nil, fmt.Errorf("constraint: empty alternative in %q", raw)
		}
		set, err := parseSet(alt)
		if err != nil {
			return nil, fmt.Errorf("constraint %q: %w", raw, err)
		}
		c.sets = append(c.sets, set)
	}
	return c, nil
}

// MustConstraint parses or panics; for tests and fixtures.
func MustConstraint(s string) *Constraint {
	c, err := ParseConstraint(s)
	if err != nil {
		panic(err)
	}
	return c
}

func parseSet(s string) (comparatorSet, error) {
	var set comparatorSet
	for _, tok := range strings.Fields(s) {
		cs, err := expandToken(tok)
		if err != nil {
			return set, err
		}
		for _, cmp := range cs {
			set.comps = append(set.comps, cmp)
			if len(cmp.ver.Pre) > 0 {
				t := [3]int{cmp.ver.Major, cmp.ver.Minor, cmp.ver.Patch}
				set.preTuples = append(set.preTuples, t)
			}
		}
	}
	return set, nil
}

// expandToken expands one token (possibly ^, ~, wildcard, or a plain
// comparator) into a list of basic comparators.
func expandToken(tok string) ([]comparator, error) {
	// Comparator operators, longest first.
	for _, op := range []string{">=", "<=", "!=", ">", "<", "="} {
		if strings.HasPrefix(tok, op) {
			v, err := Parse(strings.TrimPrefix(tok, op))
			if err != nil {
				return nil, err
			}
			return []comparator{{op: op, ver: v}}, nil
		}
	}
	switch tok[0] {
	case '^':
		v, err := Parse(fillPartial(strings.TrimPrefix(tok, "^")))
		if err != nil {
			return nil, err
		}
		upper := Version{Major: v.Major + 1}
		if v.Major == 0 {
			if v.Minor == 0 {
				upper = Version{Major: 0, Minor: 0, Patch: v.Patch + 1} // ^0.0.3
			} else {
				upper = Version{Major: 0, Minor: v.Minor + 1} // ^0.2.3
			}
		}
		return []comparator{{">=", v}, {"<", upper}}, nil
	case '~':
		v, err := Parse(fillPartial(strings.TrimPrefix(tok, "~")))
		if err != nil {
			return nil, err
		}
		return []comparator{{">=", v}, {"<", Version{Major: v.Major, Minor: v.Minor + 1}}}, nil
	}
	// Wildcards and partial versions: 1.2.x, 1.x, 1, 1.2
	if strings.ContainsAny(tok, "xX*") || strings.Count(tok, ".") < 2 {
		return expandPartial(tok)
	}
	v, err := Parse(tok)
	if err != nil {
		return nil, err
	}
	return []comparator{{"=", v}}, nil
}

// fillPartial turns "1" or "1.2" into "1.0.0" / "1.2.0" for ^/~ handling.
func fillPartial(s string) string {
	for strings.Count(s, ".") < 2 {
		s += ".0"
	}
	return s
}

// expandPartial handles "1", "1.2", "1.x", "1.2.*" etc. as a range.
func expandPartial(tok string) ([]comparator, error) {
	parts := strings.Split(tok, ".")
	if len(parts) > 3 {
		return nil, fmt.Errorf("invalid partial version %q", tok)
	}
	wild := func(p string) bool { return p == "x" || p == "X" || p == "*" }
	var nums []int
	for _, p := range parts {
		if wild(p) {
			break
		}
		n := 0
		if _, err := fmt.Sscanf(p, "%d", &n); err != nil {
			return nil, fmt.Errorf("invalid partial version %q", tok)
		}
		nums = append(nums, n)
	}
	switch len(nums) {
	case 0: // "*", "x"
		return nil, nil
	case 1: // 1.x => >=1.0.0 <2.0.0
		return []comparator{
			{">=", Version{Major: nums[0]}},
			{"<", Version{Major: nums[0] + 1}},
		}, nil
	case 2: // 1.2.x => >=1.2.0 <1.3.0
		return []comparator{
			{">=", Version{Major: nums[0], Minor: nums[1]}},
			{"<", Version{Major: nums[0], Minor: nums[1] + 1}},
		}, nil
	default: // 1.2.3 with a wildcard never reaches here; exact partial
		return []comparator{{"=", Version{Major: nums[0], Minor: nums[1], Patch: nums[2]}}}, nil
	}
}

// Allows reports whether v satisfies the constraint, applying the
// prerelease rule.
func (c *Constraint) Allows(v Version) bool {
	for _, set := range c.sets {
		if set.allows(v) {
			return true
		}
	}
	return false
}

func (s comparatorSet) allows(v Version) bool {
	if v.IsPrerelease() && !s.admitsPrereleaseOf(v) {
		return false
	}
	for _, cmp := range s.comps {
		if !cmp.allows(v) {
			return false
		}
	}
	return true
}

func (s comparatorSet) admitsPrereleaseOf(v Version) bool {
	for _, t := range s.preTuples {
		if t[0] == v.Major && t[1] == v.Minor && t[2] == v.Patch {
			return true
		}
	}
	return false
}

func (c comparator) allows(v Version) bool {
	r := Compare(v, c.ver)
	switch c.op {
	case "=":
		return r == 0
	case "!=":
		return r != 0
	case ">":
		return r > 0
	case ">=":
		return r >= 0
	case "<":
		return r < 0
	case "<=":
		return r <= 0
	}
	return false
}

func (c *Constraint) String() string {
	if c.raw != "" {
		return c.raw
	}
	return "*"
}
