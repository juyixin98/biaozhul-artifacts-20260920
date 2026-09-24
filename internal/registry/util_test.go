package registry_test

import (
	"crypto/rand"
	"encoding/json"
	"io"

	"layer-gc/internal/digest"
)

func gcDigestOf(b []byte) string { return digest.FromBytes(b) }

func randomBytes(n int) []byte {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return b
}

func decodeJSON(r io.Reader, v any) error { return json.NewDecoder(r).Decode(v) }
