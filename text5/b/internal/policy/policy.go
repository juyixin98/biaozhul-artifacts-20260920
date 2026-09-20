// Package policy implements the privacy/monitoring policy that decides
// whether a snapshot may be persisted at all.
package policy

import (
	"strings"
	"time"
)

// Policy is one immutable published policy version.
type Policy struct {
	Version             int
	WorkStartMinutes    int // minutes after local midnight, inclusive
	WorkEndMinutes      int // exclusive
	Workdays            map[time.Weekday]bool
	ExcludedApps        map[string]bool // lower-cased app names
	ExemptDepartmentIDs map[int64]bool
}

// Allows reports whether a snapshot taken on behalf of an employee in
// department deptID, in timezone loc, at minuteUTC, for app, may enter the
// raw table and statistics. Anything rejected here is dropped before
// persistence and leaves no trace in the database.
func (p Policy) Allows(deptID int64, loc *time.Location, minuteUTC time.Time, app string) bool {
	if p.ExemptDepartmentIDs[deptID] {
		return false
	}
	if p.ExcludedApps[strings.ToLower(strings.TrimSpace(app))] {
		return false
	}
	local := minuteUTC.In(loc)
	if !p.Workdays[local.Weekday()] {
		return false
	}
	m := local.Hour()*60 + local.Minute()
	return m >= p.WorkStartMinutes && m < p.WorkEndMinutes
}

// New builds a Policy from storage-friendly values.
func New(version, startMin, endMin int, workdays []int64, excludedApps []string, exemptDepts []int64) Policy {
	p := Policy{
		Version:             version,
		WorkStartMinutes:    startMin,
		WorkEndMinutes:      endMin,
		Workdays:            make(map[time.Weekday]bool, len(workdays)),
		ExcludedApps:        make(map[string]bool, len(excludedApps)),
		ExemptDepartmentIDs: make(map[int64]bool, len(exemptDepts)),
	}
	for _, d := range workdays {
		p.Workdays[time.Weekday(d)] = true
	}
	for _, a := range excludedApps {
		p.ExcludedApps[strings.ToLower(strings.TrimSpace(a))] = true
	}
	for _, id := range exemptDepts {
		p.ExemptDepartmentIDs[id] = true
	}
	return p
}
