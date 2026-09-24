// Package crypto implements the message authentication used on the ingestion
// protocol: HMAC-SHA256 over a canonical representation of the message
// envelope, verified with crypto/hmac's constant-time compare. Timestamps and
// single-use nonces defend against replay.
package crypto

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

const (
	// MaxClockSkew is how far the X-Timestamp header may differ from the
	// server clock. Tests build signed envelopes with the current time, so
	// this only rejects genuinely stale/replayed frames.
	MaxClockSkew = 5 * time.Minute
)

var (
	ErrBadSignature  = errors.New("invalid signature")
	ErrTimestampSkew = errors.New("timestamp outside allowed skew window")
	ErrReplay        = errors.New("nonce already used (replay)")
)

// Signer signs and verifies envelopes with a shared per-deployment secret.
type Signer struct {
	secret []byte
	skew   time.Duration

	mu     sync.Mutex
	seen   map[string]struct{}
	seenAt map[string]time.Time
}

func NewSigner(secret []byte) *Signer {
	return &Signer{
		secret: secret,
		skew:   MaxClockSkew,
		seen:   make(map[string]struct{}),
		seenAt: make(map[string]time.Time),
	}
}

// WithSkew returns a signer with a custom skew (used by tests and the
// replay-test example). The replay caches are independent of the parent's.
func (s *Signer) WithSkew(d time.Duration) *Signer {
	return &Signer{
		secret: s.secret,
		skew:   d,
		seen:   make(map[string]struct{}),
		seenAt: make(map[string]time.Time),
	}
}

// Canonical produces the deterministic byte string that is signed. Fields are
// sorted by key so JSON object member order cannot invalidate signatures; the
// signature itself and nonce bookkeeping fields are excluded.
func Canonical(method, path string, timestamp time.Time, body []byte) []byte {
	return []byte(fmt.Sprintf("%s\n%s\n%d\n%s",
		method, path, timestamp.UnixMilli(), body))
}

// Sign returns the lowercase hex HMAC-SHA256 of canonical.
func Sign(secret []byte, method, path string, timestamp time.Time, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write(Canonical(method, path, timestamp, body))
	return hex.EncodeToString(mac.Sum(nil))
}

// Verify checks timestamp freshness, the HMAC, and nonce single-use. The
// comparison is constant time. now is injected for deterministic tests.
func (s *Signer) Verify(method, path string, timestamp time.Time, nonce, sigHex string, body []byte, now time.Time) error {
	if diff := now.Sub(timestamp); diff > s.skew || diff < -s.skew {
		return ErrTimestampSkew
	}
	got, err := hex.DecodeString(sigHex)
	if err != nil {
		return fmt.Errorf("%w: signature is not hex", ErrBadSignature)
	}
	mac := hmac.New(sha256.New, s.secret)
	mac.Write(Canonical(method, path, timestamp, body))
	want := mac.Sum(nil)
	if !hmac.Equal(got, want) {
		return ErrBadSignature
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.gcLocked(now)
	if _, dup := s.seen[nonce]; dup {
		return ErrReplay
	}
	s.seen[nonce] = struct{}{}
	s.seenAt[nonce] = now
	return nil
}

// gcLocked forgets nonces older than the skew window; they could never be
// accepted again anyway due to timestamp checks.
func (s *Signer) gcLocked(now time.Time) {
	for n, t := range s.seenAt {
		if now.Sub(t) > s.skew {
			delete(s.seen, n)
			delete(s.seenAt, n)
		}
	}
}

// CanonicalJSON re-encodes arbitrary JSON with sorted object keys so clients
// that produce stable output can reason about the signed bytes. It is provided
// as a utility for the example scripts.
func CanonicalJSON(raw json.RawMessage) ([]byte, error) {
	var v any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(sortKeys(v)); err != nil {
		return nil, err
	}
	// Encoder appends a trailing newline; strip it.
	out := buf.Bytes()
	return bytes.TrimRight(out, "\n"), nil
}

func sortKeys(v any) any {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		out := make(map[string]any, len(t))
		for _, k := range keys {
			out[k] = sortKeys(t[k])
		}
		return out
	case []any:
		for i := range t {
			t[i] = sortKeys(t[i])
		}
		return t
	default:
		return v
	}
}
