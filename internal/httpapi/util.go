package httpapi

import (
	"crypto/hmac"
	"crypto/subtle"
	"io"
	"net/http"
)

// maxBodyBytes caps ingest payload size (1 MiB).
const maxBodyBytes = 1 << 20

func readBody(r *http.Request) ([]byte, error) {
	return io.ReadAll(io.LimitReader(r.Body, maxBodyBytes))
}

// constantEqual compares strings in constant time without leaking which
// argument is the secret length.
func constantEqual(a, b string) bool {
	if len(a) != len(b) {
		// Still spend time proportional to a HMAC-length comparison.
		subtle.ConstantTimeCompare([]byte(a), []byte(a))
		return false
	}
	return hmac.Equal([]byte(a), []byte(b))
}
