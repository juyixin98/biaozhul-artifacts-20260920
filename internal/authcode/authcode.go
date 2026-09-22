// Package authcode generates 16-character transfer authorization codes.
//
// The alphabet excludes visually ambiguous characters (0/O, 1/I/L) so a code
// read out loud or from a screenshot is unambiguous. Codes use crypto/rand
// with rejection sampling to avoid modulo bias.
package authcode

import (
	"crypto/rand"
	"math/big"
)

// Length is the fixed code length required by the specification.
const Length = 16

const alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789" // 32 symbols, no 0/O/1/I/L

// New returns a random 16-character code.
func New() (string, error) {
	out := make([]byte, Length)
	n := big.NewInt(int64(len(alphabet)))
	for i := range out {
		idx, err := rand.Int(rand.Reader, n)
		if err != nil {
			return "", err
		}
		out[i] = alphabet[idx.Int64()]
	}
	return string(out), nil
}
