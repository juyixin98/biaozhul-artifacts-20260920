// Package digest computes the request fingerprint bound to an idempotency
// key. Two requests share an idempotency key safely only when method, path
// and canonicalized body all match.
package digest

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
)

// Of canonicalizes a JSON request body and hashes it together with method
// and path. JSON key order and insignificant whitespace are normalized, so
// {"a":1,"b":2} and {"b":2,"a":1} are the same request. Non-JSON bodies
// are rejected: this service only accepts JSON payloads.
func Of(method, path string, body []byte) (string, error) {
	canonical, err := canonicalJSON(body)
	if err != nil {
		return "", fmt.Errorf("digest: %w", err)
	}
	sum := sha256.Sum256([]byte(method + "\n" + path + "\n" + canonical))
	return hex.EncodeToString(sum[:]), nil
}

func canonicalJSON(body []byte) (string, error) {
	if len(body) == 0 {
		// Treat an empty body as the canonical null request.
		return "null", nil
	}
	var v any
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return "", fmt.Errorf("body is not valid JSON: %w", err)
	}
	if dec.More() {
		return "", fmt.Errorf("body must contain exactly one JSON value")
	}
	out, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return string(out), nil
}
