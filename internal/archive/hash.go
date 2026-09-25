package archive

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
)

// writeTarHashed writes the archive to w while simultaneously computing its
// SHA-256 digest. Bytes reaching w and bytes hashed are identical.
func writeTarHashed(w io.Writer, srcRoot string, opts *Options) (int, string, error) {
	h := sha256.New()
	n, err := WriteTar(io.MultiWriter(w, h), srcRoot, opts)
	if err != nil {
		return 0, "", err
	}
	return n, hex.EncodeToString(h.Sum(nil)), nil
}
