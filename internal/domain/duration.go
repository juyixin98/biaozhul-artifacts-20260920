package domain

import (
	"encoding/json"
	"errors"
	"time"
)

// Duration is a time.Duration that JSON-marshals as a human-readable string
// ("30s", "2m") and accepts both strings and integer nanoseconds on input.
type Duration time.Duration

func (d Duration) Std() time.Duration { return time.Duration(d) }

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(time.Duration(d).String())
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	switch x := v.(type) {
	case string:
		parsed, err := time.ParseDuration(x)
		if err != nil {
			return err
		}
		*d = Duration(parsed)
	case float64:
		*d = Duration(int64(x))
	default:
		return errors.New("duration must be a string or integer nanoseconds")
	}
	return nil
}
