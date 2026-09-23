package orset

import (
	"encoding/json"
	"sort"
)

// stateJSON is the canonical on-wire shape: tombstone tags as "origin:seq"
// strings so the document stays plain JSON.
type stateJSON struct {
	Adds    map[string][]Tag `json:"adds"`
	Removed []string         `json:"removed"`
}

// MarshalJSON emits Adds plus a sorted list of "origin:seq" tombstone tags.
func (s State) MarshalJSON() ([]byte, error) {
	out := stateJSON{Adds: s.Adds}
	for t := range s.Removed {
		out.Removed = append(out.Removed, t.String())
	}
	sort.Strings(out.Removed)
	if out.Adds == nil {
		out.Adds = map[string][]Tag{}
	}
	return json.Marshal(out)
}

// UnmarshalJSON parses the canonical shape and tolerates duplicate tags.
func (s *State) UnmarshalJSON(data []byte) error {
	var in stateJSON
	if err := json.Unmarshal(data, &in); err != nil {
		return err
	}
	s.Adds = in.Adds
	if s.Adds == nil {
		s.Adds = make(map[string][]Tag)
	}
	s.Removed = make(map[Tag]struct{}, len(in.Removed))
	for _, raw := range in.Removed {
		t, err := parseTag(raw)
		if err != nil {
			return err
		}
		s.Removed[t] = struct{}{}
	}
	return nil
}

// MarshalText renders "origin:seq" (needed when Tag appears as a map key).
func (t Tag) MarshalText() ([]byte, error) {
	return []byte(t.String()), nil
}

// UnmarshalText parses "origin:seq".
func (t *Tag) UnmarshalText(b []byte) error {
	parsed, err := parseTag(string(b))
	if err != nil {
		return err
	}
	*t = parsed
	return nil
}
