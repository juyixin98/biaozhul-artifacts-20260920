// Package crypto implements the real cryptographic operations of the
// TWAP service:
//
//   - InputHash: SHA-256 over a canonical encoding of the exact samples
//     and window parameters that produced a result. It is the version
//     identity: identical inputs produce the identical hash; any changed
//     or added late sample changes it.
//   - Sign/Verify: HMAC-SHA256 over the published version payload, so a
//     reader can authenticate that a stored version was produced by this
//     service and not tampered with in the database.
package cryptopkg

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
)

// CanonicalInputs renders the inputs of a window computation as a stable
// byte string. Samples are sorted by (timestamp, source); parameters are
// appended with explicit field labels so distinct parameter sets can
// never collide.
func CanonicalInputs(windowStart, windowEnd, staleAfterMicros int64, samples []InputSample) []byte {
	type row = InputSample
	ss := make([]row, len(samples))
	copy(ss, samples)
	sort.SliceStable(ss, func(i, j int) bool {
		if ss[i].TS != ss[j].TS {
			return ss[i].TS < ss[j].TS
		}
		return ss[i].Source < ss[j].Source
	})

	var b strings.Builder
	fmt.Fprintf(&b, "v=1\n")
	fmt.Fprintf(&b, "window_start=%d\n", windowStart)
	fmt.Fprintf(&b, "window_end=%d\n", windowEnd)
	fmt.Fprintf(&b, "stale_after_micros=%d\n", staleAfterMicros)
	fmt.Fprintf(&b, "samples:\n")
	for _, s := range ss {
		// Source is percent-quoted so embedded newlines cannot forge
		// canonical rows.
		fmt.Fprintf(&b, "%d\t%s\t%d\n", s.TS, strconv.QuoteToASCII(s.Source), s.Price)
	}
	return []byte(b.String())
}

// InputSample is the minimal sample tuple used for hashing.
type InputSample struct {
	TS     int64
	Price  int64
	Source string
}

// InputHash returns the hex SHA-256 of the canonical inputs.
func InputHash(windowStart, windowEnd, staleAfterMicros int64, samples []InputSample) string {
	sum := sha256.Sum256(CanonicalInputs(windowStart, windowEnd, staleAfterMicros, samples))
	return hex.EncodeToString(sum[:])
}

// VersionPayload is the signed representation of a published version.
type VersionPayload struct {
	WindowStart   int64  `json:"window_start"`
	WindowEnd     int64  `json:"window_end"`
	Version       int    `json:"version"`
	InputHash     string `json:"input_hash"`
	TWAPNum       string `json:"twap_num"`
	TWAPDen       string `json:"twap_den"`
	CoveredMicros int64  `json:"covered_micros"`
	WindowMicros  int64  `json:"window_micros"`
	ConflictCount int    `json:"conflict_count"`
	Stale         bool   `json:"stale"`
	LastSampleTS  int64  `json:"last_sample_ts"`
}

// canonicalPayload renders VersionPayload deterministically (encoding/
// json sorts map keys but struct field order is fixed in code; we render
// explicitly to be robust against reordering).
func canonicalPayload(p VersionPayload) []byte {
	var b strings.Builder
	fmt.Fprintf(&b, "twap-version|v1|")
	fmt.Fprintf(&b, "window_start=%d|", p.WindowStart)
	fmt.Fprintf(&b, "window_end=%d|", p.WindowEnd)
	fmt.Fprintf(&b, "version=%d|", p.Version)
	fmt.Fprintf(&b, "input_hash=%s|", p.InputHash)
	fmt.Fprintf(&b, "twap=%s/%s|", p.TWAPNum, p.TWAPDen)
	fmt.Fprintf(&b, "covered=%d/%d|", p.CoveredMicros, p.WindowMicros)
	fmt.Fprintf(&b, "conflicts=%d|", p.ConflictCount)
	fmt.Fprintf(&b, "stale=%t|", p.Stale)
	fmt.Fprintf(&b, "last_sample_ts=%d", p.LastSampleTS)
	return []byte(b.String())
}

// Sign returns hex HMAC-SHA256 of the payload under key.
func Sign(key []byte, p VersionPayload) string {
	m := hmac.New(sha256.New, key)
	m.Write(canonicalPayload(p))
	return hex.EncodeToString(m.Sum(nil))
}

// Verify checks a hex HMAC-SHA256 signature in constant time.
func Verify(key []byte, p VersionPayload, sigHex string) bool {
	expected, err := hex.DecodeString(sigHex)
	if err != nil {
		return false
	}
	m := hmac.New(sha256.New, key)
	m.Write(canonicalPayload(p))
	return hmac.Equal(expected, m.Sum(nil))
}

// GenerateKey returns 32 cryptographically random bytes (used when no
// SIGNING_KEY is configured) and must never fail silently.
func GenerateKey() ([]byte, error) {
	key := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, key); err != nil {
		return nil, fmt.Errorf("generate signing key: %w", err)
	}
	return key, nil
}
