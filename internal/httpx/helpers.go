package httpx

import (
	"strings"

	"synapticgo/internal/store"
)

// digestFromHeader parses RFC 3230 style "sha-256=<hex>". It tolerates the
// token being uppercase-prefixed but requires a lowercase hex SHA-256 value.
func digestFromHeader(h string) string {
	h = strings.TrimSpace(h)
	if h == "" {
		return ""
	}
	parts := strings.SplitN(h, "=", 2)
	if len(parts) != 2 {
		return ""
	}
	if !strings.EqualFold(strings.TrimSpace(parts[0]), "sha-256") {
		return ""
	}
	v := strings.TrimSpace(parts[1])
	v = strings.Trim(v, `"`)
	if !store.ValidateDigest(v) {
		return ""
	}
	return v
}
