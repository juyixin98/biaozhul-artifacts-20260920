// Package digest computes content digests used across the provenance system.
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"strings"
)

// Algo is the only supported digest algorithm.
const Algo = "sha256"

// Digest is an algorithm-tagged content digest.
type Digest struct {
	Algo string `json:"algo"`
	Hex  string `json:"hex"`
}

// OfBytes returns the digest of b.
func OfBytes(b []byte) Digest {
	sum := sha256.Sum256(b)
	return Digest{Algo: Algo, Hex: hex.EncodeToString(sum[:])}
}

// OfFile returns the digest of the file at path (streamed, so large
// artifacts do not need to fit in memory).
func OfFile(path string) (Digest, error) {
	f, err := os.Open(path)
	if err != nil {
		return Digest{}, err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return Digest{}, err
	}
	return Digest{Algo: Algo, Hex: hex.EncodeToString(h.Sum(nil))}, nil
}

// Parse accepts "sha256:<hex>" or a bare 64-char hex string.
func Parse(s string) (Digest, error) {
	algo, hx := Algo, s
	if i := strings.Index(s, ":"); i >= 0 {
		algo, hx = s[:i], s[i+1:]
	}
	if algo != Algo {
		return Digest{}, fmt.Errorf("unsupported digest algorithm %q", algo)
	}
	if b, err := hex.DecodeString(hx); err != nil || len(b) != sha256.Size {
		return Digest{}, fmt.Errorf("invalid %s digest %q", algo, s)
	}
	return Digest{Algo: algo, Hex: hx}, nil
}

// String renders "sha256:<hex>".
func (d Digest) String() string { return d.Algo + ":" + d.Hex }

// Equal reports whether two digests are identical.
func (d Digest) Equal(o Digest) bool { return d.Algo == o.Algo && d.Hex == o.Hex }

// IsZero reports whether d is the empty digest.
func (d Digest) IsZero() bool { return d.Algo == "" && d.Hex == "" }
