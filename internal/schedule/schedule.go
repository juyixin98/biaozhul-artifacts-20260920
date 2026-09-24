// Package schedule parses minute/hour/week-of-day expressions and computes
// future fire instants in an IANA time zone. It implements the daylight
// saving rules required by the service:
//
//   - A wall-clock minute that does not exist on a spring-forward day (the
//     clock jumps over it) is skipped entirely.
//   - A wall-clock minute that occurs twice on a fall-back day fires exactly
//     once, at the earlier of the two instants.
package schedule

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Schedule is a parsed, validated expression bound to a location.
type Schedule struct {
	MinuteExpr string
	HourExpr   string
	WeekExpr   string
	Location   *time.Location

	minutes     field
	hours       field
	weekdays    field
	minuteOfDay []int // sorted hour*60+minute matches
}

// Parse binds the three expressions to a location. Empty or "*" means "every".
// Weekdays are 0-6 with Sunday = 0 (7 is also accepted as Sunday).
func Parse(minute, hour, weekday string, loc *time.Location) (*Schedule, error) {
	if loc == nil {
		return nil, fmt.Errorf("location is required")
	}
	mins, err := parseField(minute, 0, 59, "minute")
	if err != nil {
		return nil, err
	}
	hours, err := parseField(hour, 0, 23, "hour")
	if err != nil {
		return nil, err
	}
	days, err := parseField(weekday, 0, 7, "weekday")
	if err != nil {
		return nil, err
	}
	// Normalize Sunday 7 -> 0.
	if days.has(7) {
		days.delete(7)
		days.add(0)
	}

	s := &Schedule{
		MinuteExpr: minute, HourExpr: hour, WeekExpr: weekday,
		Location: loc,
		minutes:  mins, hours: hours, weekdays: days,
	}
	for h := 0; h < 24; h++ {
		if !hours.has(h) {
			continue
		}
		for m := 0; m < 60; m++ {
			if mins.has(m) {
				s.minuteOfDay = append(s.minuteOfDay, h*60+m)
			}
		}
	}
	return s, nil
}

// Next returns the earliest fire instant strictly after after. The boolean is
// false only if no match exists within the search horizon (15 days).
func (s *Schedule) Next(after time.Time) (time.Time, bool) {
	t := after.In(s.Location).Truncate(time.Minute)
	if !t.After(after) {
		t = t.Add(time.Minute)
	}

	// Every matching weekday repeats at least every 7 days; 15 days gives a
	// full spare week even if a matching day contributes no valid instant
	// (all of its matched wall minutes can fall in a spring-forward gap).
	for day := 0; day < 15; day++ {
		y, m, d := t.Date()
		// Noon UTC always exists and shares the calendar date; weekday is a
		// property of the date, not of a zone offset.
		if s.weekdays.has(int(time.Date(y, m, d, 12, 0, 0, 0, time.UTC).Weekday())) {
			startMod := 0
			if day == 0 {
				startMod = t.Hour()*60 + t.Minute()
			}
			idx := sort.SearchInts(s.minuteOfDay, startMod)
			for ; idx < len(s.minuteOfDay); idx++ {
				mod := s.minuteOfDay[idx]
				cand, ok := resolveWall(s.Location, y, m, d, mod/60, mod%60)
				if !ok {
					// Spring-forward gap: this wall minute never happens.
					continue
				}
				if cand.After(after) {
					return cand, true
				}
			}
		}
		// First minute of the next calendar day in zone wall time.
		t = time.Date(y, m, d+1, 0, 0, 0, 0, s.Location)
	}
	return time.Time{}, false
}

// ForEachBetween calls fn for every fire instant in the half-open interval
// (after, until]. Iteration stops early if fn returns false.
func (s *Schedule) ForEachBetween(after, until time.Time, fn func(time.Time) bool) {
	t := after
	for {
		next, ok := s.Next(t)
		if !ok || !next.Before(until) && !next.Equal(until) {
			return
		}
		if !fn(next) {
			return
		}
		t = next
	}
}

// resolveWall maps wall-clock fields to a unique instant with explicit DST
// semantics instead of relying on time.Date's normalization choices:
//
//   - A gap minute (spring forward) has no valid mapping -> ok=false (skip).
//   - An ambiguous minute (fall back) maps to two instants; the EARLIEST is
//     returned so the minute executes only on its first occurrence.
//
// The candidate and the points ±24h away hold at most two distinct UTC
// offsets (no zone runs two transitions within 24h), which enumerates every
// possible mapping of the given wall minute.
func resolveWall(loc *time.Location, y int, mo time.Month, d, hour, min int) (time.Time, bool) {
	naive := time.Date(y, mo, d, hour, min, 0, 0, loc)
	offsets := map[int]struct{}{}
	for _, probe := range []time.Time{naive.Add(-24 * time.Hour), naive, naive.Add(24 * time.Hour)} {
		_, off := probe.Zone()
		offsets[off] = struct{}{}
	}
	wallUTC := time.Date(y, mo, d, hour, min, 0, 0, time.UTC).Unix()
	var best time.Time
	found := false
	for off := range offsets {
		// "wall fields with offset off" means UTC = wall - off.
		c := time.Unix(wallUTC-int64(off), 0).In(loc)
		cy, cm, cd := c.Date()
		if cy == y && cm == mo && cd == d && c.Hour() == hour && c.Minute() == min {
			if !found || c.Before(best) {
				best, found = c, true
			}
		}
	}
	return best, found
}

// ---- field set ---------------------------------------------------------

type field map[int]bool

func (f field) has(v int) bool { return f[v] }
func (f field) add(v int)      { f[v] = true }
func (f field) delete(v int)   { delete(f, v) }

// parseField understands: *  n  a-b  */k  a-b/k  and comma lists of those.
func parseField(expr string, min, max int, name string) (field, error) {
	f := field{}
	expr = strings.TrimSpace(expr)
	if expr == "" || expr == "*" {
		for v := min; v <= max; v++ {
			f.add(v)
		}
		return f, nil
	}
	for _, part := range strings.Split(expr, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return nil, fmt.Errorf("%s: empty list element", name)
		}
		step := 1
		rangePart := part
		if i := strings.Index(part, "/"); i >= 0 {
			rangePart = part[:i]
			n, err := strconv.Atoi(part[i+1:])
			if err != nil || n <= 0 {
				return nil, fmt.Errorf("%s: invalid step %q", name, part)
			}
			step = n
		}
		lo, hi := min, max
		switch {
		case rangePart == "*":
		case strings.Contains(rangePart, "-"):
			segs := strings.SplitN(rangePart, "-", 2)
			a, err1 := strconv.Atoi(segs[0])
			b, err2 := strconv.Atoi(segs[1])
			if err1 != nil || err2 != nil || a < min || b > max || a > b {
				return nil, fmt.Errorf("%s: invalid range %q", name, rangePart)
			}
			lo, hi = a, b
		default:
			v, err := strconv.Atoi(rangePart)
			if err != nil || v < min || v > max {
				return nil, fmt.Errorf("%s: invalid value %q (allowed %d-%d)", name, rangePart, min, max)
			}
			lo, hi = v, v
			if strings.Contains(part, "/") {
				// n/k means from n through max by k.
				hi = max
			}
		}
		for v := lo; v <= hi; v += step {
			f.add(v)
		}
	}
	if len(f) == 0 {
		return nil, fmt.Errorf("%s: expression matched no values", name)
	}
	return f, nil
}
