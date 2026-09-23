// Package semver implements a simplified semantic version model:
// MAJOR.MINOR.PATCH with optional -prerelease and +build metadata.
// Build metadata is parsed but ignored for precedence, as per SemVer 2.0.0.
package semver

import (
	"fmt"
	"strconv"
	"strings"
)

// Version is a parsed semantic version.
type Version struct {
	Major int
	Minor int
	Patch int
	// Pre holds the dot-separated prerelease identifiers; empty means a
	// normal (release) version, which sorts AFTER any prerelease of the
	// same MAJOR.MINOR.PATCH.
	Pre   []string
	Build string
}

// Parse parses "1.2.3", "1.2.3-alpha.1", "1.2.3+build.5".
// A leading "v" is tolerated. Missing minor/patch are NOT tolerated here
// (use ParseConstraint for partial versions in constraints).
func Parse(s string) (Version, error) {
	orig := s
	s = strings.TrimPrefix(s, "v")
	var v Version
	if i := strings.IndexByte(s, '+'); i >= 0 {
		v.Build = s[i+1:]
		s = s[:i]
		if v.Build == "" {
			return v, fmt.Errorf("semver: empty build metadata in %q", orig)
		}
	}
	if i := strings.IndexByte(s, '-'); i >= 0 {
		pre := s[i+1:]
		s = s[:i]
		if pre == "" {
			return v, fmt.Errorf("semver: empty prerelease in %q", orig)
		}
		v.Pre = strings.Split(pre, ".")
		for _, id := range v.Pre {
			if id == "" {
				return v, fmt.Errorf("semver: empty prerelease identifier in %q", orig)
			}
		}
	}
	parts := strings.Split(s, ".")
	if len(parts) != 3 {
		return v, fmt.Errorf("semver: want MAJOR.MINOR.PATCH, got %q", orig)
	}
	nums := make([]int, 3)
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 || (len(p) > 1 && p[0] == '0') {
			return v, fmt.Errorf("semver: invalid numeric component %q in %q", p, orig)
		}
		nums[i] = n
	}
	v.Major, v.Minor, v.Patch = nums[0], nums[1], nums[2]
	return v, nil
}

// MustParse parses or panics; for tests and fixtures.
func MustParse(s string) Version {
	v, err := Parse(s)
	if err != nil {
		panic(err)
	}
	return v
}

func (v Version) String() string {
	s := fmt.Sprintf("%d.%d.%d", v.Major, v.Minor, v.Patch)
	if len(v.Pre) > 0 {
		s += "-" + strings.Join(v.Pre, ".")
	}
	if v.Build != "" {
		s += "+" + v.Build
	}
	return s
}

// IsPrerelease reports whether the version has a prerelease tag.
func (v Version) IsPrerelease() bool { return len(v.Pre) > 0 }

// Compare returns -1, 0 or +1. Build metadata is ignored.
func Compare(a, b Version) int {
	if a.Major != b.Major {
		return cmpInt(a.Major, b.Major)
	}
	if a.Minor != b.Minor {
		return cmpInt(a.Minor, b.Minor)
	}
	if a.Patch != b.Patch {
		return cmpInt(a.Patch, b.Patch)
	}
	return comparePre(a.Pre, b.Pre)
}

func cmpInt(a, b int) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

// comparePre implements SemVer 2.0.0 §11: a release outranks any of its
// prereleases; identifiers compare numerically when both numeric,
// ASCII otherwise; numeric < alphanumeric; shorter set < longer set
// when all shared identifiers are equal.
func comparePre(a, b []string) int {
	if len(a) == 0 && len(b) == 0 {
		return 0
	}
	if len(a) == 0 {
		return 1 // release > prerelease
	}
	if len(b) == 0 {
		return -1
	}
	for i := 0; i < len(a) && i < len(b); i++ {
		ai, bi := a[i], b[i]
		an, aerr := strconv.Atoi(ai)
		bn, berr := strconv.Atoi(bi)
		switch {
		case aerr == nil && berr == nil:
			if an != bn {
				return cmpInt(an, bn)
			}
		case aerr == nil:
			return -1 // numeric < alphanumeric
		case berr == nil:
			return 1
		default:
			if c := strings.Compare(ai, bi); c != 0 {
				return c
			}
		}
	}
	return cmpInt(len(a), len(b))
}

// SortDescending sorts versions highest-first (solver preference order).
func SortDescending(vs []Version) {
	for i := 1; i < len(vs); i++ {
		for j := i; j > 0 && Compare(vs[j-1], vs[j]) < 0; j-- {
			vs[j-1], vs[j] = vs[j], vs[j-1]
		}
	}
}
