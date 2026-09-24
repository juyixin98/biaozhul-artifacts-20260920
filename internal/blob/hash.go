package blob

import (
	"crypto/sha256"
	"encoding/hex"
	"hash"

	"atomicpromo/internal/crypto/sig"
)

type sha struct{ h hash.Hash }

func newSHA256() *sha { return &sha{h: sha256.New()} }

func (s *sha) Write(p []byte) (int, error) { return s.h.Write(p) }

func (s *sha) digest() string {
	return sig.DigestPrefix + hex.EncodeToString(s.h.Sum(nil))
}
