package mqtt

import "strings"

// ValidTopic reports whether name is a legal PUBLISH topic name (§4.7).
// Topic names are non-empty, UTF-8, length-limited, and contain no wildcards.
func ValidTopic(name string) bool {
	if len(name) == 0 || len(name) > 65535 {
		return false
	}
	if strings.ContainsAny(name, "#+") {
		return false
	}
	if strings.ContainsRune(name, 0) {
		return false
	}
	return validMQTTUTF8(name)
}

// ValidFilter reports whether f is a legal subscription topic filter (§4.7):
// '+' matches exactly one level, '#' only appears as the final level and
// matches zero or more trailing levels (so "x/#/y" and "x#" are illegal).
func ValidFilter(f string) bool {
	if len(f) == 0 || len(f) > 65535 {
		return false
	}
	levels := strings.Split(f, "/")
	for i, lvl := range levels {
		switch {
		case lvl == "#":
			if i != len(levels)-1 { // must be the last level
				return false
			}
		case strings.ContainsRune(lvl, '#'): // '#' glued to other chars
			return false
		case strings.ContainsRune(lvl, '+') && lvl != "+": // '+' glued to chars
			return false
		}
	}
	return validMQTTUTF8(f)
}

// TopicMatch reports whether a concrete topic name matches a filter
// (§4.7.1/§4.7.2). Note §4.7.2: "sport/#" does NOT match "sport" because
// the '#' must consume a level after a separating '/'. The lone filter
// "#" does match every name (including zero-level names, which do not
// occur here because names are non-empty).
func TopicMatch(filter, name string) bool {
	f := strings.Split(filter, "/")
	n := strings.Split(name, "/")
	for i, fl := range f {
		if fl == "#" {
			// '#' is guaranteed by ValidFilter to be last; it matches
			// all remaining concrete levels, of which there must be at
			// least one (i == len(n) means a missing level → no match).
			return i < len(n)
		}
		if i >= len(n) {
			return false
		}
		if fl != "+" && fl != n[i] {
			return false
		}
	}
	return len(f) == len(n)
}
