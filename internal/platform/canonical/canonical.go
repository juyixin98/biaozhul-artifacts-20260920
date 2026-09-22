// Package canonical produces deterministic JSON for hashing: object keys are
// sorted recursively and no insignificant whitespace is emitted. Map keys are
// strings (JSON objects); []any stays ordered.
package canonical

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
)

func JSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(canonicalize(v)); err != nil {
		return nil, err
	}
	// Encode appends a newline; strip it.
	out := buf.Bytes()
	if n := len(out); n > 0 && out[n-1] == '\n' {
		out = out[:n-1]
	}
	return out, nil
}

// MustJSON panics on error; use only for values known to be JSON-safe.
func MustJSON(v any) []byte {
	b, err := JSON(v)
	if err != nil {
		panic(fmt.Sprintf("canonical: %v", err))
	}
	return b
}

// MarshalRoundTrip round-trips bytes through JSON so callers can canonicalize
// already-serialized payloads.
func MarshalRoundTrip(raw []byte) ([]byte, error) {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, err
	}
	return JSON(v)
}

func canonicalize(v any) any {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		out := make(map[string]any, len(t))
		for _, k := range keys {
			out[k] = canonicalize(t[k])
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = canonicalize(t[i])
		}
		return out
	default:
		return v
	}
}
