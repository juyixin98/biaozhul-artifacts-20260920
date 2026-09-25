package commands

import (
	"encoding/json"
	"fmt"
)

// decode converts a payload (either an already-typed struct, as used
// by library callers and tests, or a generic JSON-decoded
// map[string]any / json.RawMessage, as received over HTTP) into T.
func decode[T any](payload any) (T, error) {
	var zero T
	switch v := payload.(type) {
	case T:
		return v, nil
	case nil:
		return zero, nil
	case json.RawMessage:
		var t T
		if len(v) == 0 {
			return zero, nil
		}
		if err := json.Unmarshal(v, &t); err != nil {
			return zero, fmt.Errorf("invalid payload: %w", err)
		}
		return t, nil
	case map[string]any:
		raw, err := json.Marshal(v)
		if err != nil {
			return zero, fmt.Errorf("invalid payload: %w", err)
		}
		var t T
		if err := json.Unmarshal(raw, &t); err != nil {
			return zero, fmt.Errorf("invalid payload: %w", err)
		}
		return t, nil
	default:
		raw, err := json.Marshal(v)
		if err != nil {
			return zero, fmt.Errorf("unsupported payload type %T", payload)
		}
		var t T
		if err := json.Unmarshal(raw, &t); err != nil {
			return zero, fmt.Errorf("invalid payload: %w", err)
		}
		return t, nil
	}
}

// toInt normalizes the JSON-ish numeric results returned by tasks.
func toInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	case nil:
		return 0
	default:
		return 0
	}
}
