package policy

import (
	"regexp"
	"time"

	"desklens/internal/glob"
	"desklens/internal/repo"
	"desklens/internal/timeutil"
)

// Filter is the compiled current policy. Filters run inside ingestion before
// anything is written: rejected data never reaches the raw table and cannot
// influence any statistic.
type Filter struct {
	version       int
	windowStart   int
	windowEnd     int
	exemptDepts   map[int64]bool
	excludedGlobs []*regexp.Regexp
}

func NewFilter(p repo.Policy) (*Filter, error) {
	f := &Filter{
		version:     p.Version,
		windowStart: p.WindowStartMinute,
		windowEnd:   p.WindowEndMinute,
		exemptDepts: map[int64]bool{},
	}
	for _, d := range p.ExemptDepartments {
		f.exemptDepts[d] = true
	}
	for _, pat := range p.ExcludedPatterns {
		re, err := glob.Compile(pat)
		if err != nil {
			return nil, err
		}
		f.excludedGlobs = append(f.excludedGlobs, re)
	}
	return f, nil
}

func (f *Filter) Version() int { return f.version }

// ExemptDepartment reports whether monitoring is disabled for the department
// in this policy version.
func (f *Filter) ExemptDepartment(departmentID int64) bool {
	return f.exemptDepts[departmentID]
}

// ExcludedApp reports whether the app is on the privacy exclusion list
// (case-insensitive local glob match).
func (f *Filter) ExcludedApp(appName string) bool {
	name := appName
	for _, re := range f.excludedGlobs {
		if re.MatchString(lower(name)) {
			return true
		}
	}
	return false
}

// InWindow reports whether the minute falls inside the employee's monitoring
// window in the employee's own timezone. start == end means a whole-day
// window; start < end is a normal interval; start > end wraps past midnight.
func (f *Filter) InWindow(minuteUTC time.Time, loc *time.Location) bool {
	if f.windowStart == f.windowEnd {
		return true
	}
	m := timeutil.MinuteOfDay(minuteUTC, loc)
	if f.windowStart < f.windowEnd {
		return m >= f.windowStart && m < f.windowEnd
	}
	return m >= f.windowStart || m < f.windowEnd
}

func lower(s string) string {
	b := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		b[i] = c
	}
	return string(b)
}
