package dataset

import (
	"crypto/sha256"
	"encoding/hex"
	"hash"
	"io"
)

// sha256Writer wraps an io.Writer while accumulating a hash.
type sha256Writer struct {
	w io.Writer
	h hash.Hash
}

func newSHA256Writer(w io.Writer) *sha256Writer {
	return &sha256Writer{w: w, h: sha256.New()}
}

func (s *sha256Writer) Write(p []byte) (int, error) {
	n, err := s.w.Write(p)
	if n > 0 {
		// io.Writer contract: n <= len(p)
		s.h.Write(p[:n])
	}
	return n, err
}

func (s *sha256Writer) checksum() string {
	return hex.EncodeToString(s.h.Sum(nil))
}
