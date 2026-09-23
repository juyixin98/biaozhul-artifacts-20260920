package api

import (
	"encoding/json"
	"fmt"
	"strconv"
	"time"
)

// flexTime unmarshals a Unix timestamp in seconds (a JSON number) or an
// RFC3339/ISO8601 timestamp string. Internally everything is seconds since the
// Unix epoch, so the domain core stays unit-agnostic.
type flexTime float64

// UnmarshalJSON accepts number (Unix seconds; decimals allowed) or string
// (RFC3339 with optional ' ' separator between date and time).
func (f *flexTime) UnmarshalJSON(data []byte) error {
	var num float64
	if err := json.Unmarshal(data, &num); err == nil {
		*f = flexTime(num)
		return nil
	}
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return fmt.Errorf("time must be a Unix-seconds number or RFC3339 string, got %s", string(data))
	}
	t, err := parseTime(s)
	if err != nil {
		return err
	}
	*f = flexTime(t)
	return nil
}

func parseTime(s string) (float64, error) {
	if s == "" {
		return 0, fmt.Errorf("empty time")
	}
	if v, err := strconv.ParseFloat(s, 64); err == nil {
		return v, nil
	}
	layouts := []string{time.RFC3339Nano, time.RFC3339, "2006-01-02T15:04:05", "2006-01-02 15:04:05"}
	for _, l := range layouts {
		if t, err := time.Parse(l, s); err == nil {
			return float64(t.UnixNano()) / 1e9, nil
		}
	}
	return 0, fmt.Errorf("unrecognized time %q (use Unix seconds or RFC3339)", s)
}
