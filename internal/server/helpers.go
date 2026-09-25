package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"hash"
	"io"
	"net/http"
)

// decodeJSONLimit strictly decodes a JSON request body (unknown fields
// rejected) capped at maxBytes.
func decodeJSONLimit(w http.ResponseWriter, r *http.Request, v any, maxBytes int64) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if dec.More() {
		return io.ErrUnexpectedEOF
	}
	return nil
}

// constSpace is an injected SpaceChecker reporting a fixed free byte count,
// used to deterministically exercise the space preflight.
type constSpace int64

func (c constSpace) AvailableBytes(string) (int64, error) { return int64(c), nil }

// sha256Writer is a convenience io.Writer that also yields a hex digest.
type sha256Writer struct{ h hash.Hash }

func newHasher() *sha256Writer { return &sha256Writer{h: sha256.New()} }

func (s *sha256Writer) Write(p []byte) (int, error) { return s.h.Write(p) }

func (s *sha256Writer) hex() string { return hex.EncodeToString(s.h.Sum(nil)) }
